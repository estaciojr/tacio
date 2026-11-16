"""Pack frete: frete cobrado do cliente, faturado pela transportadora, pago a terceiros, cargas e custo por placa.

Conceito (parâmetros em ctx.regra("frete"), ex.: tenants/dalla.frete.toml):
- Arrecadado: TGFCAB.VLRFRETE das vendas (não há item de frete). A venda de entrega futura e a
  remessa carregam o mesmo frete; a base 'remessa' (data da entrega) é a padrão. Devolução desconta.
- Transportadora do grupo: emite CT-e/NFS-e para a LOJA (parceiro = filial), não para o cliente.
  A remessa aponta o documento em AD_NUNOTACTE. É frete interno ao grupo.
- Pago: CT-e recebido vira título na TGFFIN sem nota (TOPs de CT-e), pela data de negociação.
  CT-e de parceiro do grupo fica fora. Importado e ainda não processado: TGFIXN STATUS 0.
- Cargas: remessa com ORDEMCARGA > 0 ligada à TGFORD (veículo e motorista da ordem de carga).
- Custo por placa: rateio por veículo da TGFRAV (na prática combustível) + nota de compra com
  TGFCAB.CODVEICULO sem rateio (manutenção) + agregados (AD_ORDPAGMOT, líquido da ordem rateado
  pelo peso de cada placa). Custo sem veículo no lançamento (IPVA, seguro, pedágio...) vai à parte.
- KM: checklist do veículo (AD_CHLVEI), digitado em milhares e com erros; leitura fora do padrão
  deixa o km rodado em branco.
Depende de campos e tabelas personalizados (AD_NUNOTACTE, AD_ORDPAGMOT*, AD_CHLVEI).
"""
from dataclasses import dataclass
from datetime import date, timedelta

from ...pack import Contexto, Pack, filtro_in
from ...policy import PoliticaViolada, validar_periodo

pack = Pack("frete", "Frete cobrado, faturado pela transportadora, pago, cargas e custo por placa")

_KM = "(CASE WHEN K.KM < 10000 THEN K.KM * 1000 ELSE K.KM END)"  # checklist digitado em milhares
_KM_RODADO_MAX = 30000


@dataclass
class Regra:
    remessa: list[int]
    venda: list[int]
    direta: list[int]
    devolucao: list[int]
    transportadora: int
    tops_transportadora: list[int]
    cte: list[int]
    cte_ecommerce: list[int]
    internos: list[int]
    combustivel: list[int]
    sem_placa: list[int]
    tipos_veiculo: list[str]
    excluidos: list[int]


def _ints(d: dict, chave: str) -> list[int]:
    return sorted(int(v) for v in d.get(chave, []))


def _regra(ctx: Contexto) -> Regra:
    d = ctx.regra("frete").get("frete", {})
    # TOPs, naturezas e códigos são interpolados no SQL: só inteiros (e tipos de uma letra) passam daqui.
    tipos = [str(t) for t in d.get("tipos_veiculo", [])]
    if any(len(t) != 1 or not t.isalpha() for t in tipos):
        raise PoliticaViolada("tipos_veiculo da regra de frete deve ter só letras (ex.: C, R).")
    r = Regra(_ints(d, "tops_remessa"), _ints(d, "tops_venda"), _ints(d, "tops_venda_direta"),
              _ints(d, "tops_devolucao"), int(d.get("empresa_transportadora", 0)), _ints(d, "tops_transportadora"),
              _ints(d, "tops_cte"), _ints(d, "tops_cte_ecommerce"), _ints(d, "parceiros_internos"),
              _ints(d, "naturezas_combustivel"), _ints(d, "naturezas_frota_sem_placa"), tipos,
              _ints(d, "veiculos_excluidos"))
    if not r.remessa or not r.tops_transportadora or not (r.cte or r.cte_ecommerce):
        raise PoliticaViolada("Regra de frete incompleta: tops_remessa, tops_transportadora e tops_cte.")
    return r


def _lista(valores: list[int]) -> str:
    return ",".join(map(str, sorted(valores) or [0]))


def _num(v) -> float:
    return round(float(v or 0), 2)


def _meses(ini: date, fim: date) -> list[tuple[str, date, date]]:
    """Fatia o período em meses de calendário: [('AAAA-MM', início, fim), ...]."""
    fatias, d = [], ini
    while d <= fim:
        prox = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        fatias.append((f"{d:%Y-%m}", d, min(fim, prox - timedelta(days=1))))
        d = prox
    return fatias


def _validar_agrupar(agrupar: str, validos) -> None:
    if agrupar not in validos:
        raise PoliticaViolada(f"agrupar inválido. Válidos: {', '.join(validos)}.")


def _detalhe(ctx: Contexto, cte: str, modelo: str, medidas: str, params: list, top: int,
             where: str = "") -> tuple[list[dict], bool]:
    top = max(1, min(top, ctx.max_linhas))
    ranking = "{top}" in modelo
    sql = cte + modelo.format(top=f"TOP {top}", medidas=medidas, where=where)
    linhas, trunc = ctx.sql.consultar(sql, params, top if ranking else ctx.max_linhas)
    for l in linhas:
        for col in l:
            if col.startswith(("VLR_", "PESO", "KM")):
                l[col] = _num(l[col])
    return linhas, trunc


# ---------------------------------------------------------------- arrecadado

_MEDIDAS_ARRECADADO = """
    SUM(CASE WHEN N.SINAL = 1 THEN 1 ELSE 0 END)                AS NOTAS,
    SUM(CASE WHEN N.SINAL = 1 THEN N.VLRFRETE ELSE 0 END)       AS VLR_COBRADO,
    SUM(CASE WHEN N.SINAL = -1 THEN N.VLRFRETE ELSE 0 END)      AS VLR_DEVOLVIDO,
    SUM(N.SINAL * N.VLRFRETE)                                   AS VLR_LIQUIDO"""

# Modelos com {top} são ranking (corte é esperado); sem {top} são série no tempo (corte é aviso).
_AGRUPAMENTOS_ARRECADADO = {
    "mes": "SELECT CONVERT(CHAR(7), N.DTNEG, 120) AS MES, {medidas} FROM N GROUP BY CONVERT(CHAR(7), N.DTNEG, 120) ORDER BY MES",
    "dia": "SELECT CAST(N.DTNEG AS DATE) AS DIA, {medidas} FROM N GROUP BY CAST(N.DTNEG AS DATE) ORDER BY DIA",
    "empresa": """
        SELECT {top} N.CODEMP, EMP.NOMEFANTASIA AS EMPRESA, {medidas}
        FROM N JOIN sankhya.TSIEMP EMP ON EMP.CODEMP = N.CODEMP
        GROUP BY N.CODEMP, EMP.NOMEFANTASIA ORDER BY VLR_LIQUIDO DESC""",
    "operacao": """
        SELECT {top} N.CODTIPOPER, MAX(TPO.DESCROPER) AS OPERACAO, {medidas}
        FROM N JOIN sankhya.TGFTOP TPO ON TPO.CODTIPOPER = N.CODTIPOPER AND TPO.DHALTER = N.DHTIPOPER
        GROUP BY N.CODTIPOPER ORDER BY VLR_LIQUIDO DESC""",
}


@pack.tool(descricao="""
Frete cobrado do cliente nas vendas, menos o frete devolvido. Use para 'quanto arrecadamos de frete',
'frete por loja', 'frete cobrado no mês'.
- base: remessa (padrão, data da entrega) | venda (data da venda). As duas têm o mesmo frete: nunca some.
- agrupar: mes (padrão) | dia | empresa | operacao.
Empresa = loja que vendeu. Datas em AAAA-MM-DD, período máximo de 366 dias.
""")
def frete_arrecadado(ctx: Contexto, data_inicio: str, data_fim: str, codemp: int | None = None,
                     agrupar: str = "mes", base: str = "remessa", top: int = 50) -> dict:
    r = _regra(ctx)
    _validar_agrupar(agrupar, _AGRUPAMENTOS_ARRECADADO)
    if base not in ("remessa", "venda"):
        raise PoliticaViolada("base inválida. Válidas: remessa, venda.")
    ini, fim = validar_periodo(data_inicio, data_fim)
    filtro_emp, p_emp = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    tops = (r.remessa if base == "remessa" else r.venda) + r.direta
    cte = f"""
    WITH N AS (
        SELECT CAB.CODEMP, CAB.CODTIPOPER, CAB.DHTIPOPER, CAB.DTNEG, CAB.VLRFRETE,
               CASE WHEN CAB.CODTIPOPER IN ({_lista(r.devolucao)}) THEN -1 ELSE 1 END AS SINAL
        FROM sankhya.TGFCAB CAB
        WHERE CAB.CODTIPOPER IN ({_lista(tops + r.devolucao)}) AND CAB.STATUSNOTA = 'L' AND CAB.VLRFRETE <> 0
          AND CAB.DTNEG >= ? AND CAB.DTNEG < DATEADD(DAY, 1, ?){filtro_emp}
    ) """
    params = [ini, fim, *p_emp]
    linhas, trunc = _detalhe(ctx, cte, _AGRUPAMENTOS_ARRECADADO[agrupar], _MEDIDAS_ARRECADADO, params, top)
    total = ctx.sql.consultar(f"{cte} SELECT {_MEDIDAS_ARRECADADO} FROM N", params, 1)[0][0]
    resumo = {"notas": int(total["NOTAS"] or 0), "vlr_cobrado": _num(total["VLR_COBRADO"]),
              "vlr_devolvido": _num(total["VLR_DEVOLVIDO"]), "vlr_liquido": _num(total["VLR_LIQUIDO"])}
    return ctx.politica.resultado(linhas, trunc, periodo=f"{ini} a {fim}", base=base, agrupar=agrupar, resumo=resumo)


# ---------------------------------------------------------------- transportadora

_MEDIDAS_TRANSP = "COUNT(*) AS DOCUMENTOS, SUM(D.VLRNOTA) AS VLR_EMITIDO"
_AGRUPAMENTOS_TRANSP = {
    "mes": "SELECT CONVERT(CHAR(7), D.DTNEG, 120) AS MES, {medidas} FROM D GROUP BY CONVERT(CHAR(7), D.DTNEG, 120) ORDER BY MES",
    "loja": """
        SELECT {top} D.LOJA AS CODEMP, D.NOMELOJA AS EMPRESA, {medidas}
        FROM D GROUP BY D.LOJA, D.NOMELOJA ORDER BY VLR_EMITIDO DESC""",
    "documento": """
        SELECT {top} D.CODTIPOPER, MAX(TPO.DESCROPER) AS DOCUMENTO, {medidas}
        FROM D JOIN sankhya.TGFTOP TPO ON TPO.CODTIPOPER = D.CODTIPOPER AND TPO.DHALTER = D.DHTIPOPER
        GROUP BY D.CODTIPOPER ORDER BY VLR_EMITIDO DESC""",
}


@pack.tool(descricao="""
O que a transportadora do grupo faturou para as lojas (CT-e e NFS-e) e se bate com o frete das
remessas. Use para 'faturamento da transportadora', 'CT-e emitidos', 'frete interno por loja'.
- agrupar: mes (padrão) | loja | documento. codemp = loja tomadora.
- 'conciliacao': frete das remessas do período e as remessas com frete sem documento ligado.
É frete entre empresas do grupo: no consolidado se anula com a despesa da loja.
Datas em AAAA-MM-DD, período máximo de 366 dias.
""")
def frete_transportadora(ctx: Contexto, data_inicio: str, data_fim: str, codemp: int | None = None,
                         agrupar: str = "mes", top: int = 50) -> dict:
    r = _regra(ctx)
    _validar_agrupar(agrupar, _AGRUPAMENTOS_TRANSP)
    ini, fim = validar_periodo(data_inicio, data_fim)
    ctx.politica.validar_codemp(r.transportadora)
    filtro_loja, p_loja = filtro_in("LJ.CODEMP", ctx.politica.validar_codemp(codemp))
    cte = f"""
    WITH D AS (
        SELECT CAB.CODTIPOPER, CAB.DHTIPOPER, CAB.DTNEG, CAB.VLRNOTA, LJ.CODEMP AS LOJA, LJ.NOMEFANTASIA AS NOMELOJA
        FROM sankhya.TGFCAB CAB
        OUTER APPLY (SELECT TOP 1 E.CODEMP, E.NOMEFANTASIA FROM sankhya.TSIEMP E
                      WHERE E.CODPARC = CAB.CODPARC ORDER BY E.CODEMP) LJ
        WHERE CAB.CODEMP = {r.transportadora} AND CAB.CODTIPOPER IN ({_lista(r.tops_transportadora)})
          AND CAB.STATUSNOTA = 'L' AND CAB.DTNEG >= ? AND CAB.DTNEG < DATEADD(DAY, 1, ?){filtro_loja}
    ) """
    params = [ini, fim, *p_loja]
    linhas, trunc = _detalhe(ctx, cte, _AGRUPAMENTOS_TRANSP[agrupar], _MEDIDAS_TRANSP, params, top)
    total = ctx.sql.consultar(f"{cte} SELECT {_MEDIDAS_TRANSP} FROM D", params, 1)[0][0]
    filtro_rem, p_rem = filtro_in("R.CODEMP", ctx.politica.validar_codemp(codemp))
    conc = ctx.sql.consultar(f"""
        SELECT COUNT(*) AS REMESSAS, SUM(R.VLRFRETE) AS VLR_FRETE,
               SUM(CASE WHEN R.AD_NUNOTACTE IS NULL THEN 1 ELSE 0 END) AS SEM_DOCUMENTO,
               SUM(CASE WHEN R.AD_NUNOTACTE IS NULL THEN R.VLRFRETE ELSE 0 END) AS VLR_SEM_DOCUMENTO
        FROM sankhya.TGFCAB R
        WHERE R.CODTIPOPER IN ({_lista(r.remessa)}) AND R.STATUSNOTA = 'L' AND R.VLRFRETE > 0
          AND R.DTNEG >= ? AND R.DTNEG < DATEADD(DAY, 1, ?){filtro_rem}""", [ini, fim, *p_rem], 1)[0][0]
    resumo = {"documentos": int(total["DOCUMENTOS"] or 0), "vlr_emitido": _num(total["VLR_EMITIDO"])}
    conciliacao = {"remessas_com_frete": int(conc["REMESSAS"] or 0), "vlr_frete_remessas": _num(conc["VLR_FRETE"]),
                   "remessas_sem_documento": int(conc["SEM_DOCUMENTO"] or 0),
                   "vlr_sem_documento": _num(conc["VLR_SEM_DOCUMENTO"])}
    return ctx.politica.resultado(linhas, trunc, periodo=f"{ini} a {fim}", agrupar=agrupar, resumo=resumo,
                                  conciliacao=conciliacao)


# ---------------------------------------------------------------- pago

_MEDIDAS_PAGO = """
    COUNT(*)                                                    AS TITULOS,
    SUM(P.VLRDESDOB)                                            AS VLR_FRETE,
    SUM(CASE WHEN P.DHBAIXA IS NULL THEN P.VLRDESDOB ELSE 0 END) AS VLR_EM_ABERTO"""
_AGRUPAMENTOS_PAGO = {
    "mes": "SELECT CONVERT(CHAR(7), P.DTNEG, 120) AS MES, {medidas} FROM P GROUP BY CONVERT(CHAR(7), P.DTNEG, 120) ORDER BY MES",
    "empresa": """
        SELECT {top} P.CODEMP, EMP.NOMEFANTASIA AS EMPRESA, {medidas}
        FROM P JOIN sankhya.TSIEMP EMP ON EMP.CODEMP = P.CODEMP
        GROUP BY P.CODEMP, EMP.NOMEFANTASIA ORDER BY VLR_FRETE DESC""",
    "transportadora": """
        SELECT {top} P.CODPARC, PAR.NOMEPARC AS TRANSPORTADORA, {medidas}
        FROM P LEFT JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = P.CODPARC
        GROUP BY P.CODPARC, PAR.NOMEPARC ORDER BY VLR_FRETE DESC""",
    "origem": "SELECT P.ORIGEM_FRETE AS ORIGEM, {medidas} FROM P GROUP BY P.ORIGEM_FRETE ORDER BY VLR_FRETE DESC",
}


@pack.tool(descricao="""
Frete pago a transportadoras de fora do grupo (CT-e recebido), pela data de negociação do título.
Use para 'quanto pagamos de frete', 'frete por transportadora', 'frete do e-commerce'.
- origem: compras (CT-e de compra/revenda) | ecommerce (marketplace); sem origem traz as duas.
- agrupar: mes (padrão) | empresa | transportadora | origem.
- 'pendentes': CT-e importados e ainda não processados (sem título), emitidos no período.
Não inclui o frete interno da transportadora do grupo. Datas em AAAA-MM-DD, máximo de 366 dias.
""")
def frete_pago(ctx: Contexto, data_inicio: str, data_fim: str, codemp: int | None = None, agrupar: str = "mes",
               origem: str | None = None, top: int = 50) -> dict:
    r = _regra(ctx)
    _validar_agrupar(agrupar, _AGRUPAMENTOS_PAGO)
    origens = {"compras": r.cte, "ecommerce": r.cte_ecommerce}
    if origem is not None and origem not in origens:
        raise PoliticaViolada("origem inválida. Válidas: compras, ecommerce.")
    tops = origens[origem] if origem else r.cte + r.cte_ecommerce
    ini, fim = validar_periodo(data_inicio, data_fim)
    codemps = ctx.politica.validar_codemp(codemp)
    filtro_emp, p_emp = filtro_in("F.CODEMP", codemps)
    cte = f"""
    WITH P AS (
        SELECT F.CODEMP, F.CODPARC, F.DTNEG, F.DHBAIXA, F.VLRDESDOB,
               CASE WHEN F.CODTIPOPER IN ({_lista(r.cte_ecommerce)}) THEN 'ecommerce' ELSE 'compras' END AS ORIGEM_FRETE
        FROM sankhya.TGFFIN F
        WHERE F.RECDESP = -1 AND F.PROVISAO = 'N' AND F.CODTIPOPER IN ({_lista(tops)})
          AND F.CODPARC NOT IN ({_lista(r.internos)}) AND F.DTNEG >= ? AND F.DTNEG < DATEADD(DAY, 1, ?){filtro_emp}
    ) """
    params = [ini, fim, *p_emp]
    linhas, trunc = _detalhe(ctx, cte, _AGRUPAMENTOS_PAGO[agrupar], _MEDIDAS_PAGO, params, top)
    por_origem, _ = _detalhe(ctx, cte, _AGRUPAMENTOS_PAGO["origem"], _MEDIDAS_PAGO, params, 5)
    filtro_x, p_x = filtro_in("X.CODEMP", codemps)
    pend = ctx.sql.consultar(f"""
        SELECT COUNT(*) AS CTES, SUM(X.VLRNOTA) AS VLR
        FROM sankhya.TGFIXN X
        WHERE X.TIPO = 'C' AND X.STATUS = 0 AND X.CODTIPOPER IN ({_lista(tops)})
          AND ISNULL(X.CODPARC, 0) NOT IN ({_lista(r.internos)})
          AND X.DHEMISS >= ? AND X.DHEMISS < DATEADD(DAY, 1, ?){filtro_x}""", [ini, fim, *p_x], 1)[0][0]
    resumo = {"vlr_frete": _num(sum(l["VLR_FRETE"] for l in por_origem)),
              "vlr_em_aberto": _num(sum(l["VLR_EM_ABERTO"] for l in por_origem)),
              "por_origem": {l["ORIGEM"]: {"titulos": int(l["TITULOS"]), "vlr": l["VLR_FRETE"]} for l in por_origem},
              "pendentes": {"ctes": int(pend["CTES"] or 0), "vlr": _num(pend["VLR"])}}
    return ctx.politica.resultado(linhas, trunc, periodo=f"{ini} a {fim}", origem=origem, agrupar=agrupar,
                                  resumo=resumo)


# ---------------------------------------------------------------- cargas

_MEDIDAS_CARGAS = """
    COUNT(DISTINCT C.ORDEMCARGA) AS CARGAS,
    COUNT(*)                     AS NOTAS,
    SUM(C.PESO)                  AS PESO_KG,
    SUM(C.VLRFRETE)              AS VLR_FRETE"""
_AGRUPAMENTOS_CARGAS = {
    "dia": "SELECT CAST(C.DTNEG AS DATE) AS DIA, {medidas} FROM C GROUP BY CAST(C.DTNEG AS DATE) ORDER BY DIA",
    "veiculo": """
        SELECT {top} C.CODVEICULO, RTRIM(VEI.PLACA) AS PLACA, RTRIM(VEI.MARCAMODELO) AS MODELO, {medidas}
        FROM C LEFT JOIN sankhya.TGFVEI VEI ON VEI.CODVEICULO = C.CODVEICULO
        GROUP BY C.CODVEICULO, VEI.PLACA, VEI.MARCAMODELO ORDER BY VLR_FRETE DESC""",
    "motorista": """
        SELECT {top} C.CODPARCMOTORISTA AS CODMOTORISTA, ISNULL(MOT.NOMEPARC, 'SEM MOTORISTA') AS MOTORISTA, {medidas}
        FROM C LEFT JOIN sankhya.TGFPAR MOT ON MOT.CODPARC = C.CODPARCMOTORISTA
        GROUP BY C.CODPARCMOTORISTA, MOT.NOMEPARC ORDER BY VLR_FRETE DESC""",
    "carga": """
        SELECT {top} C.ORDEMCARGA, MIN(CAST(C.DTNEG AS DATE)) AS DTNEG, RTRIM(VEI.PLACA) AS PLACA,
               ISNULL(MOT.NOMEPARC, 'SEM MOTORISTA') AS MOTORISTA, {medidas}
        FROM C LEFT JOIN sankhya.TGFVEI VEI ON VEI.CODVEICULO = C.CODVEICULO
               LEFT JOIN sankhya.TGFPAR MOT ON MOT.CODPARC = C.CODPARCMOTORISTA
        GROUP BY C.ORDEMCARGA, VEI.PLACA, MOT.NOMEPARC ORDER BY MIN(C.DTNEG), C.ORDEMCARGA""",
}


@pack.tool(descricao="""
Cargas de entrega (ordens de carga) com notas, peso e frete das remessas. Use para 'quantas cargas',
'entregas por caminhão', 'por motorista', 'cargas do dia'.
- agrupar: dia (padrão) | veiculo | motorista | carga (lista).
Data = da remessa. Empresa = loja da remessa. Veículo e motorista = os da ordem de carga.
Datas em AAAA-MM-DD, período máximo de 92 dias.
""")
def cargas(ctx: Contexto, data_inicio: str, data_fim: str, codemp: int | None = None, agrupar: str = "dia",
           top: int = 50) -> dict:
    r = _regra(ctx)
    _validar_agrupar(agrupar, _AGRUPAMENTOS_CARGAS)
    ini, fim = validar_periodo(data_inicio, data_fim, max_dias=92)
    filtro_emp, p_emp = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    cte = f"""
    WITH C AS (
        SELECT CAB.DTNEG, CAB.ORDEMCARGA, CAB.PESO, CAB.VLRFRETE, ORD.CODVEICULO, ORD.CODPARCMOTORISTA
        FROM sankhya.TGFCAB CAB
        JOIN sankhya.TGFEMP EMO ON EMO.CODEMP = CAB.CODEMP
        JOIN sankhya.TGFORD ORD ON ORD.ORDEMCARGA = CAB.ORDEMCARGA AND ORD.CODEMP = ISNULL(EMO.CODEMPOC, EMO.CODEMP)
        WHERE CAB.CODTIPOPER IN ({_lista(r.remessa)}) AND CAB.STATUSNOTA = 'L' AND CAB.ORDEMCARGA > 0
          AND CAB.DTNEG >= ? AND CAB.DTNEG < DATEADD(DAY, 1, ?){filtro_emp}
    ) """
    params = [ini, fim, *p_emp]
    linhas, trunc = _detalhe(ctx, cte, _AGRUPAMENTOS_CARGAS[agrupar], _MEDIDAS_CARGAS, params, top)
    total = ctx.sql.consultar(f"{cte} SELECT {_MEDIDAS_CARGAS} FROM C", params, 1)[0][0]
    resumo = {"cargas": int(total["CARGAS"] or 0), "notas": int(total["NOTAS"] or 0),
              "peso_kg": _num(total["PESO_KG"]), "vlr_frete": _num(total["VLR_FRETE"])}
    return ctx.politica.resultado(linhas, trunc, periodo=f"{ini} a {fim}", agrupar=agrupar, resumo=resumo)


# ---------------------------------------------------------------- custo por placa

@pack.tool(descricao="""
Custo de cada veículo da frota comparado com o frete que ele gerou. Use para 'custo por placa',
'quanto custa o caminhão X', 'qual veículo dá prejuízo', 'custo da frota'.
- Custos: combustível (rateio por veículo), manutenção e outros (nota com o veículo) e agregado
  (motorista terceiro pago por peso: líquido da ordem de pagamento rateado pelo peso de cada placa).
- Receita: frete das remessas das cargas do veículo. KM vem do checklist (digitação manual).
- incluir_todos=true traz também carros, empilhadeiras e outros veículos fora da frota de entrega.
- 'sem_placa': custo da frota sem veículo no lançamento (IPVA, seguro, pedágio, multa,
  rastreamento). Salário de motorista próprio não está separado e fica de fora.
- agrupar: veiculo (padrão, período inteiro) | mes (uma linha por veículo e mês, KM de cada mês,
  e resumo.por_mes com o total da frota). Para evolução mês a mês, use mes numa chamada só em vez
  de chamar um mês por vez.
Datas em AAAA-MM-DD, período máximo de 366 dias.
""")
def custo_por_placa(ctx: Contexto, data_inicio: str, data_fim: str, codveiculo: int | None = None,
                    incluir_todos: bool = False, top: int = 100, agrupar: str = "veiculo") -> dict:
    _validar_agrupar(agrupar, ("veiculo", "mes"))
    r = _regra(ctx)
    ini, fim = validar_periodo(data_inicio, data_fim)
    if ctx.tenant.codemp_permitidas:
        raise PoliticaViolada("Custo por placa olha a frota do grupo inteiro; não disponível com empresas restritas.")
    comb = _lista(r.combustivel)
    filtro_vei, p_vei = "", []
    if codveiculo is not None:
        filtro_vei, p_vei = " AND V.CODVEICULO = ?", [int(codveiculo)]
    elif not incluir_todos:
        tipos = ",".join(f"'{t}'" for t in r.tipos_veiculo) or "''"
        filtro_vei = f" AND V.ATIVO = 'S' AND V.TIPO IN ({tipos}) AND V.CODVEICULO NOT IN ({_lista(r.excluidos)})"
    periodo = "C.DTNEG >= ? AND C.DTNEG < DATEADD(DAY, 1, ?)"
    sql = f"""
    WITH REC AS (
        SELECT ORD.CODVEICULO, COUNT(DISTINCT C.ORDEMCARGA) AS CARGAS, COUNT(*) AS NOTAS, SUM(C.VLRFRETE) AS VLR
        FROM sankhya.TGFCAB C
        JOIN sankhya.TGFEMP EMO ON EMO.CODEMP = C.CODEMP
        JOIN sankhya.TGFORD ORD ON ORD.ORDEMCARGA = C.ORDEMCARGA AND ORD.CODEMP = ISNULL(EMO.CODEMPOC, EMO.CODEMP)
        WHERE C.CODTIPOPER IN ({_lista(r.remessa)}) AND C.STATUSNOTA = 'L' AND C.ORDEMCARGA > 0 AND {periodo}
        GROUP BY ORD.CODVEICULO),
    CUSTO AS (
        SELECT R.CODVEICULO, R.CODNAT, C.VLRNOTA * R.PERCRATEIO / 100 AS VLR
        FROM sankhya.TGFRAV R
        JOIN sankhya.TGFCAB C ON C.NUNOTA = R.NUFIN
        JOIN sankhya.TGFTOP T ON T.CODTIPOPER = C.CODTIPOPER AND T.DHALTER = C.DHTIPOPER
        WHERE R.ORIGEM = 'E' AND R.CODVEICULO > 0 AND T.TIPMOV = 'C' AND C.STATUSNOTA = 'L' AND {periodo}
        UNION ALL
        SELECT C.CODVEICULO, C.CODNAT, C.VLRNOTA
        FROM sankhya.TGFCAB C
        JOIN sankhya.TGFTOP T ON T.CODTIPOPER = C.CODTIPOPER AND T.DHALTER = C.DHTIPOPER
        WHERE C.CODVEICULO > 0 AND T.TIPMOV = 'C' AND C.STATUSNOTA = 'L' AND {periodo}
          AND NOT EXISTS (SELECT 1 FROM sankhya.TGFRAV R WHERE R.ORIGEM = 'E' AND R.NUFIN = C.NUNOTA)),
    CST AS (
        SELECT CODVEICULO, SUM(CASE WHEN CODNAT IN ({comb}) THEN VLR ELSE 0 END) AS COMBUSTIVEL,
               SUM(CASE WHEN CODNAT IN ({comb}) THEN 0 ELSE VLR END) AS MANUTENCAO
        FROM CUSTO GROUP BY CODVEICULO),
    AGR AS (
        SELECT REPLACE(RTRIM(N.PLACA), '-', '') AS PLACA, SUM(O.VLRARECEBER * N.PESO / NULLIF(TOT.PESO, 0)) AS VLR
        FROM sankhya.AD_ORDPAGMOT O
        JOIN sankhya.AD_ORDPAGMOTNOTA N ON N.NUORDPAG = O.NUORDPAG
        CROSS APPLY (SELECT SUM(N2.PESO) AS PESO FROM sankhya.AD_ORDPAGMOTNOTA N2 WHERE N2.NUORDPAG = O.NUORDPAG) TOT
        WHERE O.SITUACAO = 'A' AND O.DTFIM >= ? AND O.DTFIM < DATEADD(DAY, 1, ?)
        GROUP BY REPLACE(RTRIM(N.PLACA), '-', '')),
    KMS AS (
        SELECT K.CODVEICULO,
               MAX(CASE WHEN K.ORD_INI = 1 THEN K.KMN END) AS KM_INICIAL,
               MAX(CASE WHEN K.ORD_FIM = 1 THEN K.KMN END) AS KM_FINAL, COUNT(*) AS LEITURAS
        FROM (SELECT K.CODVEICULO, {_KM} AS KMN,
                     ROW_NUMBER() OVER (PARTITION BY K.CODVEICULO ORDER BY K.DTCAD) AS ORD_INI,
                     ROW_NUMBER() OVER (PARTITION BY K.CODVEICULO ORDER BY K.DTCAD DESC) AS ORD_FIM
              FROM sankhya.AD_CHLVEI K
              WHERE K.KM > 0 AND K.DTCAD >= ? AND K.DTCAD < DATEADD(DAY, 1, ?)) K
        GROUP BY K.CODVEICULO)
    SELECT V.CODVEICULO, RTRIM(V.PLACA) AS PLACA, RTRIM(V.MARCAMODELO) AS MODELO, V.TIPO,
           ISNULL(REC.CARGAS, 0) AS CARGAS, ISNULL(REC.NOTAS, 0) AS NOTAS, ISNULL(REC.VLR, 0) AS VLR_FRETE,
           ISNULL(CST.COMBUSTIVEL, 0) AS VLR_COMBUSTIVEL, ISNULL(CST.MANUTENCAO, 0) AS VLR_MANUTENCAO,
           ISNULL(AGR.VLR, 0) AS VLR_AGREGADO,
           ISNULL(CST.COMBUSTIVEL, 0) + ISNULL(CST.MANUTENCAO, 0) + ISNULL(AGR.VLR, 0) AS VLR_CUSTO,
           KMS.KM_INICIAL, KMS.KM_FINAL,
           CASE WHEN KMS.LEITURAS > 1 AND KMS.KM_FINAL - KMS.KM_INICIAL BETWEEN 0 AND {_KM_RODADO_MAX}
                THEN KMS.KM_FINAL - KMS.KM_INICIAL END AS KM_RODADO
    FROM sankhya.TGFVEI V
    LEFT JOIN REC ON REC.CODVEICULO = V.CODVEICULO
    LEFT JOIN CST ON CST.CODVEICULO = V.CODVEICULO
    LEFT JOIN AGR ON AGR.PLACA = RTRIM(V.PLACA)
    LEFT JOIN KMS ON KMS.CODVEICULO = V.CODVEICULO
    WHERE V.CODVEICULO > 0{filtro_vei}
      AND (REC.CODVEICULO IS NOT NULL OR CST.CODVEICULO IS NOT NULL OR AGR.PLACA IS NOT NULL)
    ORDER BY VLR_CUSTO DESC"""
    top = max(1, min(top, ctx.max_linhas))
    # Por mês roda a mesma consulta mês a mês: KM e custo de cada mês saem com a mesma regra.
    fatias = _meses(ini, fim) if agrupar == "mes" else [(None, ini, fim)]
    linhas, trunc, por_mes = [], False, []
    for mes, f_ini, f_fim in fatias:
        parte, t = ctx.sql.consultar(sql, [f_ini, f_fim] * 5 + p_vei, top)
        trunc = trunc or t
        for l in parte:
            for col in l:
                if col.startswith(("VLR_", "KM")) and l[col] is not None:
                    l[col] = _num(l[col])
            l["PERC_CUSTO_SOBRE_FRETE"] = _num(100 * l["VLR_CUSTO"] / l["VLR_FRETE"]) if l["VLR_FRETE"] else None
            l["CUSTO_POR_KM"] = _num(l["VLR_CUSTO"] / l["KM_RODADO"]) if l.get("KM_RODADO") else None
        if mes is not None:
            parte = [{"MES": mes, **{k: v for k, v in l.items() if k != "MES"}} for l in parte]
            por_mes.append({"mes": mes, "veiculos": len(parte),
                            "vlr_frete": _num(sum(l["VLR_FRETE"] for l in parte)),
                            "vlr_custo": _num(sum(l["VLR_CUSTO"] for l in parte))})
        linhas += parte
    if len(linhas) > ctx.max_linhas:
        linhas, trunc = linhas[:ctx.max_linhas], True
    sem_placa = ctx.sql.consultar(f"""
        SELECT F.CODNAT, NAT.DESCRNAT AS NATUREZA, COUNT(*) AS TITULOS, SUM(F.VLRDESDOB) AS VLR
        FROM sankhya.TGFFIN F LEFT JOIN sankhya.TGFNAT NAT ON NAT.CODNAT = F.CODNAT
        WHERE F.RECDESP = -1 AND F.PROVISAO = 'N' AND F.CODNAT IN ({_lista(r.sem_placa)})
          AND F.DTNEG >= ? AND F.DTNEG < DATEADD(DAY, 1, ?)
        GROUP BY F.CODNAT, NAT.DESCRNAT ORDER BY VLR DESC""", [ini, fim], 50)[0] if codveiculo is None else []
    resumo = {"veiculos": len({l.get("CODVEICULO") for l in linhas}),
              "vlr_frete": _num(sum(l["VLR_FRETE"] for l in linhas)),
              "vlr_combustivel": _num(sum(l["VLR_COMBUSTIVEL"] for l in linhas)),
              "vlr_manutencao": _num(sum(l["VLR_MANUTENCAO"] for l in linhas)),
              "vlr_agregado": _num(sum(l["VLR_AGREGADO"] for l in linhas)),
              "vlr_custo": _num(sum(l["VLR_CUSTO"] for l in linhas)),
              "sem_placa": {"vlr": _num(sum(s["VLR"] or 0 for s in sem_placa)),
                            "por_natureza": [{"codnat": s["CODNAT"], "natureza": (s["NATUREZA"] or "").strip(),
                                              "titulos": int(s["TITULOS"]), "vlr": _num(s["VLR"])} for s in sem_placa]}}
    if por_mes:
        resumo["por_mes"] = por_mes
    return ctx.politica.resultado(linhas, trunc, periodo=f"{ini} a {fim}", incluir_todos=incluir_todos,
                                  agrupar=agrupar, resumo=resumo)


# ---------------------------------------------------------------- frota

@pack.tool(descricao="""
Veículos da frota com último KM do checklist, última carga e ordens de serviço. Use para
'lista da frota', 'caminhões ativos', 'veículo X', 'quais veículos estão em manutenção'.
- OS_EM_SERVICO: OS sem fim do serviço (veículo em manutenção). OS_SEM_FECHAMENTO: serviço feito ou
  não, mas a OS não foi encerrada no sistema; costuma acumular.
- incluir_todos=true traz também carros, empilhadeiras, inativos e veículos fora da frota de entrega.
KM do checklist é digitado à mão (em milhares); confira antes de usar como hodômetro.
""")
def frota(ctx: Contexto, codveiculo: int | None = None, incluir_todos: bool = False, top: int = 100) -> dict:
    r = _regra(ctx)
    filtro, params = "", []
    if codveiculo is not None:
        filtro, params = " AND V.CODVEICULO = ?", [int(codveiculo)]
    elif not incluir_todos:
        tipos = ",".join(f"'{t}'" for t in r.tipos_veiculo) or "''"
        filtro = f" AND V.ATIVO = 'S' AND V.TIPO IN ({tipos}) AND V.CODVEICULO NOT IN ({_lista(r.excluidos)})"
    top = max(1, min(top, ctx.max_linhas))
    sql = f"""
    SELECT TOP {top} V.CODVEICULO, RTRIM(V.PLACA) AS PLACA, RTRIM(V.MARCAMODELO) AS MODELO, V.ESPECIETIPO, V.TIPO,
           V.ATIVO, V.PROPRIO, V.ANOFABRIC, CK.KM_ULTIMO, CAST(CK.DTCAD AS DATE) AS DT_ULTIMO_CHECKLIST,
           CAST(OC.DTALTER AS DATE) AS DT_ULTIMA_CARGA, ISNULL(OS.EM_SERVICO, 0) AS OS_EM_SERVICO,
           ISNULL(OS.SEM_FECHAMENTO, 0) AS OS_SEM_FECHAMENTO
    FROM sankhya.TGFVEI V
    OUTER APPLY (SELECT TOP 1 {_KM} AS KM_ULTIMO, K.DTCAD FROM sankhya.AD_CHLVEI K
                  WHERE K.CODVEICULO = V.CODVEICULO AND K.KM > 0 ORDER BY K.DTCAD DESC) CK
    OUTER APPLY (SELECT MAX(O.DTALTER) AS DTALTER FROM sankhya.TGFORD O WHERE O.CODVEICULO = V.CODVEICULO) OC
    OUTER APPLY (SELECT SUM(CASE WHEN S.DTFINSERV IS NULL THEN 1 ELSE 0 END) AS EM_SERVICO,
                        COUNT(*) AS SEM_FECHAMENTO
                   FROM sankhya.AD_OSTRANSP S WHERE S.CODVEICULO = V.CODVEICULO AND S.DTFINOS IS NULL) OS
    WHERE V.CODVEICULO > 0{filtro}
    ORDER BY V.TIPO, V.PLACA"""
    linhas, trunc = ctx.sql.consultar(sql, params, top)
    for l in linhas:
        if l["KM_ULTIMO"] is not None:
            l["KM_ULTIMO"] = _num(l["KM_ULTIMO"])
    return ctx.politica.resultado(linhas, trunc, incluir_todos=incluir_todos)
