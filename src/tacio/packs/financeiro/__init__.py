"""Pack financeiro: contas a pagar e a receber, baixas, fluxo previsto e saldo de contas.

Conceito:
- Título em aberto: DHBAIXA nula, PROVISAO 'N' e RECDESP 1 (receber) ou -1 (pagar). RECDESP 0 fica
  de fora. Provisão só entra no fluxo previsto, quando pedida.
- Valor em aberto: saldo do desdobramento (multa, juro, retenções, impostos da TGFIMF etc.).
- Baixa: VLRBAIXA por DHBAIXA. Com NUBCO passou pelo caixa/banco; sem NUBCO é compensação entre
  títulos, que não movimenta dinheiro e fica fora por padrão.
- Parceiro é o do título: no cartão é a adquirente, não o cliente da nota.
- Saldo: abertura mensal da TGFSBC mais o movimento da TGFMBC desde ela. É o saldo no ERP, não o
  extrato do banco; conta sem movimento recente pode estar com saldo antigo.
- Segmentos (opcional, ctx.regra("financeiro"), ex.: tenants/dalla.financeiro.toml): naturezas que
  ficam fora de aberto e fluxo e só entram quando pedidas ou ao filtrar parceiro/natureza.
"""
from dataclasses import dataclass

from ...pack import Contexto, Pack, filtro_in
from ...policy import PoliticaViolada, validar_periodo

pack = Pack("financeiro", "Contas a pagar e receber, baixas, fluxo previsto e saldo de contas")

_TIPOS = {"receber": 1, "pagar": -1}
_HOJE = "CAST(GETDATE() AS DATE)"
_VLR = """(ISNULL(F.VLRDESDOB, 0)
    + (CASE WHEN F.TIPMULTA = '1' THEN ISNULL(F.VLRMULTA, 0) ELSE 0 END)
    + (CASE WHEN F.TIPJURO = '1' THEN ISNULL(F.VLRJURO, 0) ELSE 0 END)
    + ISNULL(F.DESPCART, 0) + ISNULL(F.VLRVENDOR, 0) - ISNULL(F.VLRDESC, 0)
    - (CASE WHEN F.IRFRETIDO = 'S' THEN ISNULL(F.VLRIRF, 0) ELSE 0 END)
    - (CASE WHEN F.ISSRETIDO = 'S' THEN ISNULL(F.VLRISS, 0) ELSE 0 END)
    - (CASE WHEN F.INSSRETIDO = 'S' THEN ISNULL(F.VLRINSS, 0) ELSE 0 END)
    - ISNULL(F.CARTAODESC, 0) + ISNULL(IMF.VLRIMP, 0)
    + ISNULL(F.VLRMULTANEGOC, 0) + ISNULL(F.VLRJURONEGOC, 0)
    - ISNULL(F.VLRMULTALIB, 0) - ISNULL(F.VLRJUROLIB, 0) + ISNULL(F.VLRVARCAMBIAL, 0))"""
_JOIN_IMF = ("OUTER APPLY (SELECT ROUND(SUM(I.VALOR * I.TIPIMP), 2) AS VLRIMP FROM sankhya.TGFIMF I "
             "WHERE I.NUFIN = F.NUFIN) IMF")

_MEDIDAS_ABERTO = """
    COUNT(*)                                                     AS TITULOS,
    SUM(A.VLR)                                                   AS VLR_ABERTO,
    SUM(CASE WHEN A.DIAS_ATRASO > 0 THEN A.VLR ELSE 0 END)       AS VLR_VENCIDO,
    SUM(CASE WHEN A.DIAS_ATRASO > 90 THEN A.VLR ELSE 0 END)      AS VLR_VENCIDO_MAIS_90,
    SUM(CASE WHEN ISNULL(A.DIAS_ATRASO, 0) <= 0 THEN A.VLR ELSE 0 END) AS VLR_A_VENCER"""

# Modelos com {top} são ranking (corte é esperado); sem {top} são série no tempo (corte é aviso).
_POR_FAIXA = "SELECT A.FAIXA, {medidas} FROM A GROUP BY A.FAIXA ORDER BY MIN(ISNULL(A.DIAS_ATRASO, 0)) DESC"
_AGRUPAMENTOS_ABERTO = {
    "empresa": """
        SELECT {top} A.CODEMP, EMP.NOMEFANTASIA AS EMPRESA, {medidas}
        FROM A JOIN sankhya.TSIEMP EMP ON EMP.CODEMP = A.CODEMP {where}
        GROUP BY A.CODEMP, EMP.NOMEFANTASIA ORDER BY VLR_ABERTO DESC""",
    "natureza": """
        SELECT {top} A.CODNAT, NAT.DESCRNAT AS NATUREZA, {medidas}
        FROM A LEFT JOIN sankhya.TGFNAT NAT ON NAT.CODNAT = A.CODNAT {where}
        GROUP BY A.CODNAT, NAT.DESCRNAT ORDER BY VLR_ABERTO DESC""",
    "parceiro": """
        SELECT {top} A.CODPARC, PAR.NOMEPARC AS PARCEIRO, {medidas}
        FROM A LEFT JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = A.CODPARC {where}
        GROUP BY A.CODPARC, PAR.NOMEPARC ORDER BY VLR_ABERTO DESC""",
    "operacao": """
        SELECT {top} A.CODTIPOPER, MAX(TPO.DESCROPER) AS OPERACAO, {medidas}
        FROM A LEFT JOIN sankhya.TGFTOP TPO ON TPO.CODTIPOPER = A.CODTIPOPER AND TPO.DHALTER = A.DHTIPOPER {where}
        GROUP BY A.CODTIPOPER ORDER BY VLR_ABERTO DESC""",
    "tipo_titulo": """
        SELECT {top} A.CODTIPTIT, TIT.DESCRTIPTIT AS TIPO_TITULO, {medidas}
        FROM A LEFT JOIN sankhya.TGFTIT TIT ON TIT.CODTIPTIT = A.CODTIPTIT {where}
        GROUP BY A.CODTIPTIT, TIT.DESCRTIPTIT ORDER BY VLR_ABERTO DESC""",
    "vencimento": """
        SELECT CAST(A.DTVENC AS DATE) AS VENCIMENTO, {medidas} FROM A {where}
        GROUP BY CAST(A.DTVENC AS DATE) ORDER BY VENCIMENTO""",
    "titulo": """
        SELECT {top} A.NUFIN, A.CODEMP, A.CODPARC, PAR.NOMEPARC AS PARCEIRO, A.NUMNOTA, A.DTNEG,
               CAST(A.DTVENC AS DATE) AS DTVENC, A.DIAS_ATRASO, A.VLR AS VLR_ABERTO, A.FAIXA,
               NAT.DESCRNAT AS NATUREZA, TIT.DESCRTIPTIT AS TIPO_TITULO, A.HISTORICO
        FROM A LEFT JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = A.CODPARC
               LEFT JOIN sankhya.TGFNAT NAT ON NAT.CODNAT = A.CODNAT
               LEFT JOIN sankhya.TGFTIT TIT ON TIT.CODTIPTIT = A.CODTIPTIT {where}
        ORDER BY A.DTVENC, A.NUFIN""",
}

_MEDIDAS_BAIXA = "COUNT(*) AS TITULOS, SUM(F.VLRBAIXA) AS VLR_BAIXA"
_AGRUPAMENTOS_BAIXA = {
    "dia": "SELECT CAST(F.DHBAIXA AS DATE) AS DIA, {medidas} FROM {de} {where} GROUP BY CAST(F.DHBAIXA AS DATE) ORDER BY DIA",
    "mes": """
        SELECT CONVERT(CHAR(7), F.DHBAIXA, 120) AS MES, {medidas} FROM {de} {where}
        GROUP BY CONVERT(CHAR(7), F.DHBAIXA, 120) ORDER BY MES""",
    "empresa": """
        SELECT {top} F.CODEMP, EMP.NOMEFANTASIA AS EMPRESA, {medidas}
        FROM {de} JOIN sankhya.TSIEMP EMP ON EMP.CODEMP = F.CODEMP {where}
        GROUP BY F.CODEMP, EMP.NOMEFANTASIA ORDER BY VLR_BAIXA DESC""",
    "natureza": """
        SELECT {top} F.CODNAT, NAT.DESCRNAT AS NATUREZA, {medidas}
        FROM {de} LEFT JOIN sankhya.TGFNAT NAT ON NAT.CODNAT = F.CODNAT {where}
        GROUP BY F.CODNAT, NAT.DESCRNAT ORDER BY VLR_BAIXA DESC""",
    "parceiro": """
        SELECT {top} F.CODPARC, PAR.NOMEPARC AS PARCEIRO, {medidas}
        FROM {de} LEFT JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = F.CODPARC {where}
        GROUP BY F.CODPARC, PAR.NOMEPARC ORDER BY VLR_BAIXA DESC""",
    "conta": """
        SELECT {top} F.CODCTABCOINT, CTA.DESCRICAO AS CONTA, {medidas}
        FROM {de} LEFT JOIN sankhya.TSICTA CTA ON CTA.CODCTABCOINT = F.CODCTABCOINT {where}
        GROUP BY F.CODCTABCOINT, CTA.DESCRICAO ORDER BY VLR_BAIXA DESC""",
    "operacao": """
        SELECT {top} F.CODTIPOPERBAIXA, MAX(TPO.DESCROPER) AS OPERACAO, {medidas}
        FROM {de} LEFT JOIN sankhya.TGFTOP TPO ON TPO.CODTIPOPER = F.CODTIPOPERBAIXA AND TPO.DHALTER = F.DHTIPOPERBAIXA {where}
        GROUP BY F.CODTIPOPERBAIXA ORDER BY VLR_BAIXA DESC""",
}

_BUCKETS_FLUXO = {
    "dia": "CAST(F.DTVENC AS DATE)",
    "semana": "DATEADD(DAY, -DATEDIFF(DAY, 0, F.DTVENC) % 7, CAST(F.DTVENC AS DATE))",
    "mes": "CONVERT(CHAR(7), F.DTVENC, 120)",
}


def _num(v) -> float:
    return round(float(v or 0), 2)


def _validar_tipo(tipo: str) -> int:
    if tipo not in _TIPOS:
        raise PoliticaViolada(f"tipo inválido. Válidos: {', '.join(_TIPOS)}.")
    return _TIPOS[tipo]


@dataclass
class Segmento:
    nome: str
    tipo: str
    naturezas: list[int]

    @property
    def filtro(self) -> str:
        return f"F.RECDESP = {_TIPOS[self.tipo]} AND F.CODNAT IN ({','.join(map(str, self.naturezas))})"


def _segmentos(ctx: Contexto) -> dict[str, Segmento]:
    if "financeiro" not in ctx.tenant.regras:
        return {}
    segs = {}
    for sid, d in ctx.regra("financeiro").get("financeiro", {}).get("segmentos", {}).items():
        # Naturezas são interpoladas no SQL: só inteiros passam daqui.
        s = Segmento(d.get("nome", sid), d.get("tipo", ""), sorted(int(n) for n in d.get("naturezas", [])))
        if s.tipo not in _TIPOS or not s.naturezas:
            raise PoliticaViolada(f"Segmento financeiro '{sid}' precisa de tipo (receber ou pagar) e naturezas.")
        segs[sid] = s
    return segs


def _fora_dos_segmentos(segs: dict[str, Segmento]) -> str:
    return "".join(f" AND NOT ({s.filtro})" for s in segs.values())


def _validar_agrupar(agrupar: str, validos) -> None:
    if agrupar not in validos:
        raise PoliticaViolada(f"agrupar inválido. Válidos: {', '.join(validos)}.")


def _filtros(codparc: int | None, codnat: int | None) -> tuple[str, list[int]]:
    where, params = "", []
    for coluna, valor in (("F.CODPARC", codparc), ("F.CODNAT", codnat)):
        if valor is not None:
            where += f" AND {coluna} = ?"
            params.append(int(valor))
    return where, params


def _detalhe(ctx: Contexto, prefixo: str, modelo: str, medidas: str, params: list, top: int,
             where: str = "", de: str = "") -> tuple[list[dict], bool]:
    top = max(1, min(top, ctx.max_linhas))
    ranking = "{top}" in modelo
    sql = prefixo + modelo.format(top=f"TOP {top}", medidas=medidas, where=where, de=de)
    linhas, trunc = ctx.sql.consultar(sql, params, top if ranking else ctx.max_linhas)
    for l in linhas:
        for col in l:
            if col.startswith("VLR_"):
                l[col] = _num(l[col])
    return linhas, trunc


@pack.tool(descricao="""
Contas a receber ou a pagar em aberto, com idade dos títulos. Use para 'quanto temos a receber',
'contas vencidas', 'maiores devedores', 'o que devemos ao fornecedor X', 'títulos do cliente Y'.
- tipo: receber | pagar.
- agrupar: faixa (padrão) | empresa | natureza | parceiro | operacao | tipo_titulo | vencimento | titulo (lista).
- situacao: vencido | a_vencer; filtra os agrupamentos, exceto 'faixa'. Filtros: codemp, codparc, codnat.
- 'resumo' traz o total por faixa de vencimento. Vencido há mais de 365 dias costuma ser título
  antigo não resolvido: informe separado do vencido recente.
- segmento: só quando a pergunta for sobre um dos recortes de segmentos_financeiro; traz somente ele.
No cartão, o parceiro é a adquirente. Não inclui provisão.
""")
def contas_em_aberto(ctx: Contexto, tipo: str, codemp: int | None = None, agrupar: str = "faixa",
                     situacao: str | None = None, codparc: int | None = None, codnat: int | None = None,
                     top: int = 50, segmento: str | None = None) -> dict:
    recdesp = _validar_tipo(tipo)
    segs = _segmentos(ctx)
    if segmento is not None and (segmento not in segs or segs[segmento].tipo != tipo):
        validos = [sid for sid, s in segs.items() if s.tipo == tipo]
        raise PoliticaViolada(f"segmento inválido para {tipo}. Válidos: {', '.join(validos) or 'nenhum configurado'}.")
    if agrupar != "faixa":
        _validar_agrupar(agrupar, ["faixa", *_AGRUPAMENTOS_ABERTO])
    if situacao not in (None, "vencido", "a_vencer"):
        raise PoliticaViolada("situacao inválida. Válidas: vencido, a_vencer.")
    filtro_emp, p_emp = filtro_in("F.CODEMP", ctx.politica.validar_codemp(codemp))
    filtro, p_filtro = _filtros(codparc, codnat)
    if segmento:
        filtro += f" AND {segs[segmento].filtro}"
    elif codparc is None and codnat is None:  # perguntar pelo parceiro ou natureza traz tudo dele
        filtro += _fora_dos_segmentos(segs)
    params = [recdesp, *p_emp, *p_filtro]
    d = f"DATEDIFF(DAY, CAST(F.DTVENC AS DATE), {_HOJE})"
    cte = f"""
    WITH A AS (
        SELECT F.NUFIN, F.CODEMP, F.CODPARC, F.CODNAT, F.CODTIPOPER, F.DHTIPOPER, F.CODTIPTIT, F.NUMNOTA, F.DTNEG,
               F.DTVENC, F.HISTORICO, {_VLR} AS VLR, {d} AS DIAS_ATRASO,
               CASE WHEN F.DTVENC IS NULL THEN 'sem_vencimento'
                    WHEN {d} < -30 THEN 'a_vencer_mais_30'
                    WHEN {d} <= 0 THEN 'a_vencer_ate_30'
                    WHEN {d} <= 30 THEN 'vencido_1_30'
                    WHEN {d} <= 90 THEN 'vencido_31_90'
                    WHEN {d} <= 365 THEN 'vencido_91_365'
                    ELSE 'vencido_mais_365' END AS FAIXA
        FROM sankhya.TGFFIN F {_JOIN_IMF}
        WHERE F.RECDESP = ? AND F.DHBAIXA IS NULL AND F.PROVISAO = 'N'{filtro_emp}{filtro}
    ) """
    por_faixa, _ = _detalhe(ctx, cte, _POR_FAIXA, _MEDIDAS_ABERTO, params, 10)
    resumo = {"vlr_aberto": _num(sum(l["VLR_ABERTO"] for l in por_faixa)),
              "vlr_vencido": _num(sum(l["VLR_VENCIDO"] for l in por_faixa)),
              "vlr_a_vencer": _num(sum(l["VLR_A_VENCER"] for l in por_faixa)),
              "titulos": sum(int(l["TITULOS"]) for l in por_faixa),
              "por_faixa": {l["FAIXA"]: {"titulos": int(l["TITULOS"]), "vlr": l["VLR_ABERTO"]} for l in por_faixa}}
    linhas, trunc = por_faixa, False
    if agrupar != "faixa":
        where = {"vencido": "WHERE A.DIAS_ATRASO > 0", "a_vencer": "WHERE ISNULL(A.DIAS_ATRASO, 0) <= 0",
                 None: ""}[situacao]
        linhas, trunc = _detalhe(ctx, cte, _AGRUPAMENTOS_ABERTO[agrupar], _MEDIDAS_ABERTO, params, top, where)
    return ctx.politica.resultado(linhas, trunc, tipo=tipo, agrupar=agrupar, situacao=situacao, segmento=segmento,
                                  resumo=resumo)


@pack.tool(descricao="""
Lista os segmentos financeiros que ficam fora da conta padrão (id, descrição, tipo e naturezas).
Use somente quando a pergunta citar um desses recortes, para descobrir o valor de 'segmento' em
contas_em_aberto. Em pergunta genérica de contas a pagar ou receber, não consulte nem mencione.
""")
def segmentos_financeiro(ctx: Contexto) -> dict:
    linhas = [{"SEGMENTO": sid, "DESCRICAO": s.nome, "TIPO": s.tipo, "NATUREZAS": ",".join(map(str, s.naturezas))}
              for sid, s in _segmentos(ctx).items()]
    return ctx.politica.resultado(linhas, False)


@pack.tool(descricao="""
O que foi recebido ou pago num período, pela data da baixa. Use para 'quanto recebemos', 'quanto
pagamos', 'pagamentos por natureza', 'por fornecedor', 'por conta bancária'.
- tipo: receber | pagar.
- agrupar: dia (padrão) | mes | empresa | natureza | parceiro | conta | operacao.
- Por padrão só o que movimentou caixa/banco. incluir_compensacoes=true soma as compensações entre
  títulos; 'resumo' sempre traz os dois valores. Filtros: codemp, codparc, codnat.
Datas em AAAA-MM-DD, período máximo de 366 dias.
""")
def baixas_periodo(ctx: Contexto, tipo: str, data_inicio: str, data_fim: str, codemp: int | None = None,
                   agrupar: str = "dia", codparc: int | None = None, codnat: int | None = None,
                   incluir_compensacoes: bool = False, top: int = 50) -> dict:
    recdesp = _validar_tipo(tipo)
    _validar_agrupar(agrupar, _AGRUPAMENTOS_BAIXA)
    ini, fim = validar_periodo(data_inicio, data_fim)
    filtro_emp, p_emp = filtro_in("F.CODEMP", ctx.politica.validar_codemp(codemp))
    filtro, p_filtro = _filtros(codparc, codnat)
    base = (f"WHERE F.RECDESP = ? AND F.PROVISAO = 'N' AND F.DHBAIXA >= ? AND F.DHBAIXA < DATEADD(DAY, 1, ?)"
            f"{filtro_emp}{filtro}")
    params = [recdesp, ini, fim, *p_emp, *p_filtro]
    where = base if incluir_compensacoes else f"{base} AND F.NUBCO IS NOT NULL"
    linhas, trunc = _detalhe(ctx, "", _AGRUPAMENTOS_BAIXA[agrupar], _MEDIDAS_BAIXA, params, top, where,
                             "sankhya.TGFFIN F")
    total = ctx.sql.consultar(f"""
        SELECT SUM(CASE WHEN F.NUBCO IS NOT NULL THEN F.VLRBAIXA ELSE 0 END) AS VLR_CAIXA,
               SUM(CASE WHEN F.NUBCO IS NULL THEN F.VLRBAIXA ELSE 0 END) AS VLR_COMPENSADO,
               SUM(CASE WHEN F.NUBCO IS NOT NULL THEN 1 ELSE 0 END) AS TITULOS_CAIXA,
               SUM(CASE WHEN F.NUBCO IS NULL THEN 1 ELSE 0 END) AS TITULOS_COMPENSADOS
        FROM sankhya.TGFFIN F {base}""", params, 1)[0][0]
    resumo = {"vlr_caixa": _num(total["VLR_CAIXA"]), "vlr_compensado": _num(total["VLR_COMPENSADO"]),
              "titulos_caixa": int(total["TITULOS_CAIXA"] or 0),
              "titulos_compensados": int(total["TITULOS_COMPENSADOS"] or 0)}
    return ctx.politica.resultado(linhas, trunc, tipo=tipo, periodo=f"{ini} a {fim}", agrupar=agrupar,
                                  incluir_compensacoes=incluir_compensacoes, resumo=resumo)


@pack.tool(descricao="""
Fluxo previsto: a receber menos a pagar por vencimento, com saldo acumulado do período. Use para
'fluxo de caixa previsto', 'quanto vence esta semana', 'como fecha o mês'.
- agrupar: dia (padrão) | semana (início na segunda) | mes.
- incluir_provisao=true soma as provisões. O acumulado começa em zero e não inclui saldo em conta
  nem títulos já vencidos antes do período; 'vencidos_antes' mostra quanto ficou de fora.
Datas em AAAA-MM-DD, período máximo de 366 dias.
""")
def fluxo_previsto(ctx: Contexto, data_inicio: str, data_fim: str, codemp: int | None = None,
                   agrupar: str = "dia", incluir_provisao: bool = False) -> dict:
    _validar_agrupar(agrupar, _BUCKETS_FLUXO)
    ini, fim = validar_periodo(data_inicio, data_fim)
    filtro_emp, p_emp = filtro_in("F.CODEMP", ctx.politica.validar_codemp(codemp))
    provisao = "" if incluir_provisao else " AND F.PROVISAO = 'N'"
    fora = _fora_dos_segmentos(_segmentos(ctx))
    base = f"FROM sankhya.TGFFIN F {_JOIN_IMF} WHERE F.RECDESP IN (1, -1) AND F.DHBAIXA IS NULL{filtro_emp}{fora}"
    bucket = _BUCKETS_FLUXO[agrupar]
    sql = f"""
        SELECT {bucket} AS PERIODO,
               SUM(CASE WHEN F.RECDESP = 1 THEN {_VLR} ELSE 0 END) AS VLR_RECEBER,
               SUM(CASE WHEN F.RECDESP = -1 THEN {_VLR} ELSE 0 END) AS VLR_PAGAR
        {base}{provisao} AND F.DTVENC >= ? AND F.DTVENC < DATEADD(DAY, 1, ?)
        GROUP BY {bucket} ORDER BY PERIODO"""
    linhas, trunc = ctx.sql.consultar(sql, [*p_emp, ini, fim], ctx.max_linhas)
    acumulado = 0.0
    for l in linhas:
        l["VLR_RECEBER"], l["VLR_PAGAR"] = _num(l["VLR_RECEBER"]), _num(l["VLR_PAGAR"])
        l["VLR_SALDO"] = _num(l["VLR_RECEBER"] - l["VLR_PAGAR"])
        acumulado = _num(acumulado + l["VLR_SALDO"])
        l["VLR_ACUMULADO"] = acumulado
    antes = ctx.sql.consultar(f"""
        SELECT SUM(CASE WHEN F.RECDESP = 1 THEN {_VLR} ELSE 0 END) AS VLR_RECEBER,
               SUM(CASE WHEN F.RECDESP = -1 THEN {_VLR} ELSE 0 END) AS VLR_PAGAR
        {base} AND F.PROVISAO = 'N' AND F.DTVENC < ?""", [*p_emp, ini], 1)[0][0]
    resumo = {"vlr_receber": _num(sum(l["VLR_RECEBER"] for l in linhas)),
              "vlr_pagar": _num(sum(l["VLR_PAGAR"] for l in linhas)), "vlr_saldo": acumulado,
              "vencidos_antes": {"vlr_receber": _num(antes["VLR_RECEBER"]), "vlr_pagar": _num(antes["VLR_PAGAR"])}}
    return ctx.politica.resultado(linhas, trunc, periodo=f"{ini} a {fim}", agrupar=agrupar,
                                  incluir_provisao=incluir_provisao, resumo=resumo)


@pack.tool(descricao="""
Saldo atual das contas no ERP (bancos, aplicações, tesouraria e caixas). Use para 'saldo dos bancos',
'quanto temos em caixa'.
- classe: C corrente | A aplicação | G garantida | X tesouraria | Z caixa de operador.
- 'resumo' traz o total por classe; contas zeradas e inativas ficam de fora.
É o saldo registrado no ERP, não o extrato: ULTIMO_LANCAMENTO antigo indica conta que pode estar com
saldo desatualizado. Ao totalizar, avise sobre essas contas.
""")
def saldo_contas(ctx: Contexto, codemp: int | None = None, classe: str | None = None, top: int = 100) -> dict:
    if classe is not None and classe not in ("C", "A", "G", "X", "Z"):
        raise PoliticaViolada("classe inválida. Válidas: C, A, G, X, Z.")
    filtro_emp, params = filtro_in("C.CODEMP", ctx.politica.validar_codemp(codemp))
    if classe:
        filtro_emp += " AND C.CLASSE = ?"
        params.append(classe)
    cte = f"""
    WITH S AS (
        SELECT C.CODCTABCOINT, C.DESCRICAO AS CONTA, C.CLASSE, C.CODEMP,
               ISNULL(ABERT.SALDOREAL, 0) + ISNULL(MOV.VALOR, 0) AS VLR_SALDO, ULT.DTLANC AS ULTIMO_LANCAMENTO
        FROM sankhya.TSICTA C
        OUTER APPLY (SELECT TOP 1 SB.REFERENCIA, SB.SALDOREAL FROM sankhya.TGFSBC SB
                      WHERE SB.CODCTABCOINT = C.CODCTABCOINT AND SB.REFERENCIA <= {_HOJE}
                      ORDER BY SB.REFERENCIA DESC) ABERT
        OUTER APPLY (SELECT SUM(B.VLRLANC * B.RECDESP) AS VALOR FROM sankhya.TGFMBC B
                      WHERE B.CODCTABCOINT = C.CODCTABCOINT AND B.DTLANC >= ISNULL(ABERT.REFERENCIA, '19000101')
                        AND B.DTLANC < DATEADD(DAY, 1, {_HOJE})) MOV
        OUTER APPLY (SELECT MAX(B.DTLANC) AS DTLANC FROM sankhya.TGFMBC B
                      WHERE B.CODCTABCOINT = C.CODCTABCOINT AND B.DTLANC < DATEADD(DAY, 1, {_HOJE})) ULT
        WHERE C.ATIVA = 'S'{filtro_emp}
    ) """
    top = max(1, min(top, ctx.max_linhas))
    linhas, trunc = ctx.sql.consultar(
        f"{cte} SELECT TOP {top} * FROM S WHERE S.VLR_SALDO <> 0 ORDER BY ABS(S.VLR_SALDO) DESC", params, top)
    for l in linhas:
        l["VLR_SALDO"] = _num(l["VLR_SALDO"])
    por_classe, _ = ctx.sql.consultar(
        f"{cte} SELECT S.CLASSE, COUNT(*) AS CONTAS, SUM(S.VLR_SALDO) AS VLR_SALDO FROM S "
        "WHERE S.VLR_SALDO <> 0 GROUP BY S.CLASSE", params, 10)
    resumo = {l["CLASSE"]: {"contas": int(l["CONTAS"]), "vlr_saldo": _num(l["VLR_SALDO"])} for l in por_classe}
    return ctx.politica.resultado(linhas, trunc, resumo=resumo)
