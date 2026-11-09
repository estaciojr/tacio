"""Pack compras: análise de compras e fornecedores.

Estas tools são o padrão de referência para qualquer pack novo: SQL parametrizado,
escopo de CODEMP pela política, retorno via ctx.politica.resultado().

"O que é compra" não está no código: vem da classificação de TOPs do tenant
(ctx.classificacao, ex.: tenants/dalla.tops.toml). Por padrão as tools usam as naturezas
marcadas como compra (na Dalla, só 'revenda'); o agente pode pedir outras, como
'transferencia' ou 'bonificacao'. Natureza com sinal -1 (devolução) subtrai.
"""
from datetime import date

from ...pack import Contexto, Pack, filtro_in
from ...policy import PoliticaViolada, validar_periodo

pack = Pack("compras", "Análise de compras, transferências e fornecedores")

_LANCADA = "CAB.STATUSNOTA = 'L'"
# Entradas (C) e devoluções de compra (E): universo das checagens de classificação.
_TIPMOV_ENTRADA = "CAB.TIPMOV IN ('C', 'E')"
# Parceiros que são empresas do grupo (TSIEMP.CODPARC); usado em LEFT JOIN como GRP.
_JOIN_GRUPO = ("LEFT JOIN (SELECT DISTINCT EMP.CODPARC FROM sankhya.TSIEMP EMP) GRP "
               "ON GRP.CODPARC = CAB.CODPARC")


def _in_tops(tops: list[int]) -> str:
    # TOPs vêm da classificação, já convertidas para int na carga: seguro interpolar.
    return ",".join(str(int(t)) for t in tops)


def _tops_das_naturezas(ctx: Contexto, naturezas: list[str] | None) -> tuple[list[str], list[int]]:
    nats = ctx.classificacao.validar(naturezas)
    tops = ctx.classificacao.tops_de(nats)
    if not tops:
        raise PoliticaViolada(f"Nenhuma TOP classificada nas naturezas {', '.join(nats)}.")
    return nats, tops


def _movimento_por_top(ctx: Contexto, ini: date, fim: date, codemp: int | None) -> list[dict]:
    """Notas/valor por TOP de entrada no período, com quantas notas têm parceiro do grupo."""
    filtro_emp, p_emp = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    liberados = ctx.classificacao.checagens.parceiros_grupo_fornecedores
    cond_liberado = (f"CAB.CODPARC IN ({','.join(str(int(c)) for c in liberados)})"
                     if liberados else "1 = 0")
    # Para cada natureza ligada a um campo S/N da TOP, conta as notas com o campo = 'S'.
    flags = "".join(f", SUM(CASE WHEN TPO.{campo} = 'S' THEN 1 ELSE 0 END) AS TOPFLAG_{i}"
                    for i, campo in enumerate(ctx.classificacao.checagens.campos_top.values()))
    sql = f"""
        SELECT CAB.CODTIPOPER,
               COUNT(*)                    AS QTD_NOTAS,
               SUM(CAB.VLRNOTA)            AS VLR_NOTAS,
               SUM(CASE WHEN GRP.CODPARC IS NOT NULL THEN 1 ELSE 0 END) AS NOTAS_PARC_GRUPO,
               SUM(CASE WHEN {cond_liberado} THEN 1 ELSE 0 END) AS NOTAS_GRUPO_FORNECEDOR{flags}
        FROM sankhya.TGFCAB CAB
        JOIN sankhya.TGFTOP TPO ON TPO.CODTIPOPER = CAB.CODTIPOPER AND TPO.DHALTER = CAB.DHTIPOPER
        {_JOIN_GRUPO}
        WHERE {_LANCADA} AND {_TIPMOV_ENTRADA}
          AND CAB.DTNEG >= ? AND CAB.DTNEG < DATEADD(DAY, 1, ?){filtro_emp}
        GROUP BY CAB.CODTIPOPER
    """
    linhas, _ = ctx.sql.consultar(sql, [ini, fim, *p_emp], max_linhas=1000)
    return linhas


def _avisos(ctx: Contexto, movimento: list[dict]) -> list[str]:
    """Checagens da classificação: avisa em vez de contar errado em silêncio."""
    cl, chk = ctx.classificacao, ctx.classificacao.checagens
    avisos = []
    for m in movimento:
        top, notas, grupo = int(m["CODTIPOPER"]), int(m["QTD_NOTAS"]), int(m["NOTAS_PARC_GRUPO"] or 0)
        grupo_fornecedor = int(m.get("NOTAS_GRUPO_FORNECEDOR") or 0)
        nat = cl.natureza_da_top(top)
        if nat is None and chk.avisar_top_nao_classificada:
            avisos.append(f"TOP {top} não classificada: {notas} notas no período ficaram fora da conta.")
        elif nat == "revenda" and grupo - grupo_fornecedor > 0 and chk.revenda_sem_parceiro_do_grupo:
            avisos.append(f"TOP {top} (revenda): {grupo - grupo_fornecedor} de {notas} notas têm "
                          "parceiro que é empresa do grupo.")
        elif nat == "transferencia" and grupo < notas and chk.transferencia_so_entre_empresas:
            avisos.append(f"TOP {top} (transferência): {notas - grupo} de {notas} notas com parceiro de fora do grupo.")
        for i, (nat_campo, campo) in enumerate(chk.campos_top.items()):
            marcadas = int(m.get(f"TOPFLAG_{i}") or 0)
            if nat == nat_campo and marcadas < notas:
                avisos.append(f"TOP {top} classificada como {nat_campo}, mas {campo} = 'N' na TOP "
                              f"em {notas - marcadas} de {notas} notas.")
            elif nat is not None and nat != nat_campo and marcadas:
                avisos.append(f"TOP {top} classificada como {nat}, mas {campo} = 'S' na TOP "
                              f"em {marcadas} de {notas} notas.")
    return avisos


@pack.tool(descricao="""
Últimas entradas de um produto: data, empresa, TOP, natureza, parceiro, quantidade e custo unitário.
Por padrão só compras de revenda; use naturezas=["transferencia"] para ver de onde veio
por transferência, ou combine (ex.: ["revenda", "transferencia"]).
Use para 'quanto pagamos da última vez' ou 'de quem compramos'.
""")
def ultimas_compras_produto(ctx: Contexto, codprod: int, quantidade: int = 5,
                            codemp: int | None = None,
                            naturezas: list[str] | None = None) -> dict:
    if not 1 <= quantidade <= 50:
        raise PoliticaViolada("quantidade deve estar entre 1 e 50.")
    nats, tops = _tops_das_naturezas(ctx, naturezas)
    filtro_emp, p_emp = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    sql = f"""
        SELECT TOP {quantidade}
               CAB.DTNEG, CAB.CODEMP, CAB.NUNOTA, CAB.NUMNOTA, CAB.CODTIPOPER,
               PAR.CODPARC, PAR.NOMEPARC,
               ITE.QTDNEG, ITE.VLRUNIT, ITE.VLRTOT
        FROM sankhya.TGFCAB CAB
        JOIN sankhya.TGFITE ITE ON ITE.NUNOTA = CAB.NUNOTA
        JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = CAB.CODPARC
        WHERE {_LANCADA} AND CAB.CODTIPOPER IN ({_in_tops(tops)})
          AND ITE.CODPROD = ?{filtro_emp}
        ORDER BY CAB.DTNEG DESC, CAB.NUNOTA DESC
    """
    linhas, trunc = ctx.sql.consultar(sql, [codprod, *p_emp], ctx.max_linhas)
    for l in linhas:
        l["NATUREZA"] = ctx.classificacao.natureza_da_top(int(l["CODTIPOPER"]))
    return ctx.politica.resultado(linhas, trunc, codprod=codprod, naturezas=nats)


@pack.tool(descricao="""
Total por fornecedor (parceiro) num período: valor das notas, número de notas,
ordenado do maior para o menor. Datas em AAAA-MM-DD (data de negociação).
Por padrão só compras de revenda; naturezas com sinal negativo (devolução) subtraem.
Inclui 'avisos' quando há TOP de entrada não classificada ou fora do padrão.
""")
def compras_por_fornecedor(ctx: Contexto, data_inicio: str, data_fim: str,
                           codemp: int | None = None, top: int = 20,
                           naturezas: list[str] | None = None) -> dict:
    ini, fim = validar_periodo(data_inicio, data_fim)
    top = max(1, min(top, ctx.max_linhas))
    nats, tops = _tops_das_naturezas(ctx, naturezas)
    negativas = ctx.classificacao.tops_negativas(tops)
    sinal = (f"CASE WHEN CAB.CODTIPOPER IN ({_in_tops(negativas)}) THEN -1 ELSE 1 END"
             if negativas else "1")
    filtro_emp, p_emp = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    sql = f"""
        SELECT TOP {top}
               PAR.CODPARC, PAR.NOMEPARC,
               SUM(CAB.VLRNOTA * {sinal})  AS VLR_COMPRADO,
               COUNT(*)                    AS QTD_NOTAS
        FROM sankhya.TGFCAB CAB
        JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = CAB.CODPARC
        WHERE {_LANCADA} AND CAB.CODTIPOPER IN ({_in_tops(tops)})
          AND CAB.DTNEG >= ? AND CAB.DTNEG < DATEADD(DAY, 1, ?){filtro_emp}
        GROUP BY PAR.CODPARC, PAR.NOMEPARC
        ORDER BY VLR_COMPRADO DESC
    """
    linhas, trunc = ctx.sql.consultar(sql, [ini, fim, *p_emp], ctx.max_linhas)
    avisos = _avisos(ctx, _movimento_por_top(ctx, ini, fim, codemp))
    return ctx.politica.resultado(linhas, trunc, periodo=f"{ini} a {fim}", naturezas=nats,
                                  avisos=avisos)


@pack.tool(descricao="""
Resumo das entradas por natureza num período: revenda, transferência, bonificação, serviço,
consumo, devolução de compra etc., com número de notas e valor. Use para 'quanto entrou',
'quanto foi compra e quanto foi transferência' ou para conferir a classificação.
Devoluções aparecem com valor negativo. Inclui 'avisos' de TOPs não classificadas.
""")
def entradas_por_natureza(ctx: Contexto, data_inicio: str, data_fim: str,
                          codemp: int | None = None) -> dict:
    ini, fim = validar_periodo(data_inicio, data_fim)
    cl = ctx.classificacao
    movimento = _movimento_por_top(ctx, ini, fim, codemp)
    totais: dict[str, dict] = {}
    for m in movimento:
        nat = cl.natureza_da_top(int(m["CODTIPOPER"]))
        if nat is None:
            continue
        sinal = cl.naturezas[nat].sinal
        t = totais.setdefault(nat, {"NATUREZA": nat, "DESCRICAO": cl.naturezas[nat].nome,
                                    "CONTA_COMO_COMPRA": cl.naturezas[nat].compra,
                                    "QTD_NOTAS": 0, "VLR_NOTAS": 0.0, "TOPS": []})
        t["QTD_NOTAS"] += int(m["QTD_NOTAS"])
        t["VLR_NOTAS"] += sinal * float(m["VLR_NOTAS"] or 0)
        t["TOPS"].append(int(m["CODTIPOPER"]))
    linhas = sorted(totais.values(), key=lambda t: -abs(t["VLR_NOTAS"]))
    for t in linhas:
        t["VLR_NOTAS"] = round(t["VLR_NOTAS"], 2)
        t["TOPS"] = ",".join(str(x) for x in sorted(t["TOPS"]))
    return ctx.politica.resultado(linhas, False, periodo=f"{ini} a {fim}",
                                  avisos=_avisos(ctx, movimento))


# --- pedidos de compra ------------------------------------------------------------
_HOJE = "CAST(GETDATE() AS DATE)"
# Nota de entrada digitada e não confirmada há mais que isso vira aviso.
_DIAS_RECEBIMENTO_PARADO = 30

# Universo de pedidos_pendentes, item a item. Regras (conferidas no banco em 19/10/2026):
# - TGFITE.QTDENTREGUE já desconta notas de entrada só digitadas (STATUSNOTA <> 'L'), então
#   "a faturar" = sem nota nenhuma e "em recebimento" = em nota ainda não confirmada (TGFVAR).
# - Pedido não confirmado (STATUSNOTA 'A') fica fora da conta; os que têm liberação pendente
#   na TSILIB aparecem à parte como aguardando autorização.
# - DTPREVENT só existe no cabeçalho: o atraso é do pedido, não do item.
_CTE_PEDIDOS = f"""
WITH ITENS AS (
    SELECT CAB.NUNOTA, CAB.CODEMP, CAB.CODTIPOPER, CAB.STATUSNOTA, CAB.DTNEG, CAB.DTPREVENT,
           CAB.CODPARC, ITE.SEQUENCIA, ITE.CODPROD, ITE.QTDNEG, ITE.QTDENTREGUE, ITE.VLRUNIT,
           CASE WHEN ITE.PENDENTE = 'S' AND ITE.QTDNEG > ITE.QTDENTREGUE
                THEN ITE.QTDNEG - ITE.QTDENTREGUE ELSE 0 END AS QTD_A_FATURAR,
           ISNULL(REC.QTD, 0) AS QTD_EM_RECEBIMENTO, REC.DTNOTA AS DT_NOTA_RECEBIMENTO,
           LIB.DHSOLICIT, LIB.CODUSUSOLICIT
    FROM sankhya.TGFCAB CAB
    JOIN sankhya.TGFITE ITE ON ITE.NUNOTA = CAB.NUNOTA
    LEFT JOIN (SELECT V.NUNOTAORIG, V.SEQUENCIAORIG, SUM(V.QTDATENDIDA) AS QTD,
                      MIN(NF.DTNEG) AS DTNOTA
               FROM sankhya.TGFVAR V
               JOIN sankhya.TGFCAB NF ON NF.NUNOTA = V.NUNOTA
               WHERE NF.STATUSNOTA <> 'L' AND NF.TIPMOV <> 'O'
               GROUP BY V.NUNOTAORIG, V.SEQUENCIAORIG) REC
           ON REC.NUNOTAORIG = ITE.NUNOTA AND REC.SEQUENCIAORIG = ITE.SEQUENCIA
    LEFT JOIN (SELECT L.NUCHAVE, MIN(L.DHSOLICIT) AS DHSOLICIT, MIN(L.CODUSUSOLICIT) AS CODUSUSOLICIT
               FROM sankhya.TSILIB L
               WHERE L.TABELA = 'TGFCAB' AND L.DHLIB IS NULL AND ISNULL(L.REPROVADO, 'N') <> 'S'
               GROUP BY L.NUCHAVE) LIB
           ON LIB.NUCHAVE = CAB.NUNOTA
    WHERE CAB.TIPMOV = 'O' AND CAB.STATUSNOTA IN ('L', 'A') AND CAB.CODTIPOPER IN ({{tops}})
      AND ((ITE.PENDENTE = 'S' AND ITE.QTDNEG > ITE.QTDENTREGUE) OR REC.QTD > 0){{filtros}}
), BASE AS (
    SELECT I.*,
           I.QTD_A_FATURAR * I.VLRUNIT      AS VLR_SALDO,
           I.QTD_EM_RECEBIMENTO * I.VLRUNIT AS VLR_EM_RECEBIMENTO,
           CASE WHEN I.QTD_A_FATURAR > 0 AND I.DTPREVENT < {_HOJE}
                THEN DATEDIFF(DAY, I.DTPREVENT, {_HOJE}) ELSE 0 END AS DIAS_ATRASO,
           CASE WHEN I.STATUSNOTA = 'L' THEN 'confirmado'
                WHEN I.DHSOLICIT IS NOT NULL THEN 'aguardando_autorizacao'
                ELSE 'nao_confirmado' END AS SITUACAO_PEDIDO
    FROM ITENS I
)
"""

_SITUACOES = {
    "todos": "1 = 1",
    "atrasados": "B.DIAS_ATRASO > 0",
    "no_prazo": f"B.QTD_A_FATURAR > 0 AND B.DTPREVENT >= {_HOJE}",
    "sem_previsao": "B.QTD_A_FATURAR > 0 AND B.DTPREVENT IS NULL",
    "em_recebimento": "B.QTD_EM_RECEBIMENTO > 0",
}

_JOIN_NOMES = ("JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = B.CODPARC "
               "JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = B.CODPROD")

_AGRUPAMENTOS = {
    "item": """
        SELECT B.NUNOTA, B.SEQUENCIA, B.CODEMP, B.CODTIPOPER, B.DTNEG, B.DTPREVENT, B.DIAS_ATRASO,
               B.CODPARC, PAR.NOMEPARC, B.CODPROD, PRO.DESCRPROD, B.QTDNEG, B.QTDENTREGUE,
               B.QTD_A_FATURAR, B.QTD_EM_RECEBIMENTO, B.DT_NOTA_RECEBIMENTO, B.VLRUNIT,
               B.VLR_SALDO, B.VLR_EM_RECEBIMENTO
        FROM BASE B {join} WHERE {cond}
        ORDER BY B.DIAS_ATRASO DESC, B.VLR_SALDO DESC, B.NUNOTA, B.SEQUENCIA""",
    "pedido": """
        SELECT B.NUNOTA, B.CODEMP, B.CODTIPOPER, B.DTNEG, B.DTPREVENT,
               MAX(B.DIAS_ATRASO) AS DIAS_ATRASO, B.CODPARC, PAR.NOMEPARC,
               COUNT(*) AS ITENS, SUM(B.VLR_SALDO) AS VLR_SALDO,
               SUM(B.VLR_EM_RECEBIMENTO) AS VLR_EM_RECEBIMENTO
        FROM BASE B {join} WHERE {cond}
        GROUP BY B.NUNOTA, B.CODEMP, B.CODTIPOPER, B.DTNEG, B.DTPREVENT, B.CODPARC, PAR.NOMEPARC
        ORDER BY DIAS_ATRASO DESC, VLR_SALDO DESC""",
    "fornecedor": """
        SELECT B.CODPARC, PAR.NOMEPARC, COUNT(DISTINCT B.NUNOTA) AS PEDIDOS, COUNT(*) AS ITENS,
               SUM(B.VLR_SALDO) AS VLR_SALDO,
               SUM(CASE WHEN B.DIAS_ATRASO > 0 THEN B.VLR_SALDO ELSE 0 END) AS VLR_ATRASADO,
               SUM(B.VLR_EM_RECEBIMENTO) AS VLR_EM_RECEBIMENTO, MAX(B.DIAS_ATRASO) AS MAIOR_ATRASO_DIAS
        FROM BASE B {join} WHERE {cond}
        GROUP BY B.CODPARC, PAR.NOMEPARC
        ORDER BY VLR_SALDO DESC""",
    "produto": f"""
        SELECT B.CODPROD, PRO.DESCRPROD, COUNT(DISTINCT B.NUNOTA) AS PEDIDOS,
               SUM(B.QTD_A_FATURAR) AS QTD_A_FATURAR, SUM(B.QTD_EM_RECEBIMENTO) AS QTD_EM_RECEBIMENTO,
               SUM(B.VLR_SALDO) AS VLR_SALDO,
               MIN(CASE WHEN B.QTD_A_FATURAR > 0 AND B.DTPREVENT >= {_HOJE}
                        THEN B.DTPREVENT END) AS PROXIMA_PREVISAO,
               MAX(B.DIAS_ATRASO) AS MAIOR_ATRASO_DIAS
        FROM BASE B {{join}} WHERE {{cond}}
        GROUP BY B.CODPROD, PRO.DESCRPROD
        ORDER BY VLR_SALDO DESC""",
}

_RESUMO = f"""
    SELECT B.SITUACAO_PEDIDO, COUNT(DISTINCT B.NUNOTA) AS PEDIDOS, COUNT(*) AS ITENS,
           SUM(B.VLR_SALDO) AS VLR_SALDO,
           SUM(CASE WHEN B.DIAS_ATRASO > 0 THEN B.VLR_SALDO ELSE 0 END) AS VLR_ATRASADO,
           COUNT(DISTINCT CASE WHEN B.DIAS_ATRASO > 0 THEN B.NUNOTA END) AS PEDIDOS_ATRASADOS,
           SUM(B.VLR_EM_RECEBIMENTO) AS VLR_EM_RECEBIMENTO,
           COUNT(DISTINCT CASE WHEN B.QTD_A_FATURAR > 0 AND B.DTPREVENT IS NULL
                               THEN B.NUNOTA END) AS PEDIDOS_SEM_PREVISAO,
           COUNT(DISTINCT CASE WHEN B.QTD_EM_RECEBIMENTO > 0
                                AND B.DT_NOTA_RECEBIMENTO < DATEADD(DAY, -{_DIAS_RECEBIMENTO_PARADO}, {_HOJE})
                               THEN B.NUNOTA END) AS PEDIDOS_RECEBIMENTO_PARADO
    FROM BASE B
    GROUP BY B.SITUACAO_PEDIDO"""

_AGUARDANDO = f"""
    SELECT B.NUNOTA, B.CODEMP, B.CODTIPOPER, B.DTNEG, B.DTPREVENT, B.CODPARC, PAR.NOMEPARC,
           COUNT(*) AS ITENS, SUM(B.VLR_SALDO) AS VLR_SALDO,
           B.DHSOLICIT, DATEDIFF(DAY, B.DHSOLICIT, {_HOJE}) AS DIAS_AGUARDANDO,
           USU.NOMEUSU AS SOLICITANTE
    FROM BASE B
    JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = B.CODPARC
    LEFT JOIN sankhya.TSIUSU USU ON USU.CODUSU = B.CODUSUSOLICIT
    WHERE B.SITUACAO_PEDIDO = 'aguardando_autorizacao'
    GROUP BY B.NUNOTA, B.CODEMP, B.CODTIPOPER, B.DTNEG, B.DTPREVENT, B.CODPARC, PAR.NOMEPARC,
             B.DHSOLICIT, USU.NOMEUSU
    ORDER BY B.DHSOLICIT"""


def _pedidos_nao_classificados(ctx: Contexto, filtro_emp: str, p_emp: list[int]) -> list[str]:
    classificadas = sorted(ctx.classificacao.tops_pedido)
    fora = f" AND CAB.CODTIPOPER NOT IN ({_in_tops(classificadas)})" if classificadas else ""
    sql = f"""
        SELECT CAB.CODTIPOPER, COUNT(DISTINCT CAB.NUNOTA) AS PEDIDOS
        FROM sankhya.TGFCAB CAB
        JOIN sankhya.TGFITE ITE ON ITE.NUNOTA = CAB.NUNOTA
        WHERE CAB.TIPMOV = 'O' AND CAB.STATUSNOTA = 'L'
          AND ITE.PENDENTE = 'S' AND ITE.QTDNEG > ITE.QTDENTREGUE{fora}{filtro_emp}
        GROUP BY CAB.CODTIPOPER
    """
    linhas, _ = ctx.sql.consultar(sql, p_emp, max_linhas=200)
    return [f"TOP de pedido {int(l['CODTIPOPER'])} não classificada: {int(l['PEDIDOS'])} "
            "pedidos pendentes ficaram fora da conta." for l in linhas]


def _num(v) -> float:
    return round(float(v or 0), 2)


def _reais(v) -> str:
    return f"R$ {_num(v):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


@pack.tool(descricao="""
Pedidos de compra em aberto: o que foi pedido ao fornecedor e ainda não chegou.
Por padrão só pedidos de revenda; outras naturezas em 'naturezas' (veja naturezas_compra,
coluna TOPS_PEDIDO: encomenda, sellers, bonificacao, consumo, servico, transferencia...).
- QTD_A_FATURAR: o fornecedor ainda não mandou nota. QTD_EM_RECEBIMENTO: já tem nota
  digitada, mas não confirmada (mercadoria chegando/conferindo).
- situacao: todos | atrasados (previsão de entrega vencida) | no_prazo | sem_previsao | em_recebimento.
- agrupar: pedido (padrão) | fornecedor | produto | item. Com nunota ou codprod, o padrão é item.
Só pedidos confirmados entram na conta e no 'resumo'. Os não confirmados com liberação
pendente vêm em 'aguardando_autorizacao'. VLR_SALDO = saldo x valor unitário, sem IPI/ST.
""")
def pedidos_pendentes(ctx: Contexto, codemp: int | None = None, codparc: int | None = None,
                      codprod: int | None = None, nunota: int | None = None,
                      naturezas: list[str] | None = None, situacao: str = "todos",
                      agrupar: str | None = None, top: int = 50) -> dict:
    if situacao not in _SITUACOES:
        raise PoliticaViolada(f"situacao inválida. Válidas: {', '.join(_SITUACOES)}.")
    if agrupar is None:
        agrupar = "item" if (nunota or codprod) else "pedido"
    if agrupar not in _AGRUPAMENTOS:
        raise PoliticaViolada(f"agrupar inválido. Válidos: {', '.join(_AGRUPAMENTOS)}.")
    top = max(1, min(top, ctx.max_linhas))
    nats = ctx.classificacao.validar(naturezas)
    tops = ctx.classificacao.tops_pedido_de(nats)
    if not tops:
        raise PoliticaViolada(f"Nenhuma TOP de pedido classificada nas naturezas {', '.join(nats)}.")

    filtro_emp, p_emp = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    filtros, params = filtro_emp, list(p_emp)
    for coluna, valor in (("CAB.CODPARC", codparc), ("ITE.CODPROD", codprod), ("CAB.NUNOTA", nunota)):
        if valor is not None:
            filtros += f" AND {coluna} = ?"
            params.append(int(valor))
    cte = _CTE_PEDIDOS.format(tops=_in_tops(tops), filtros=filtros)

    cond = f"B.SITUACAO_PEDIDO = 'confirmado' AND {_SITUACOES[situacao]}"
    detalhe = _AGRUPAMENTOS[agrupar].format(join=_JOIN_NOMES, cond=cond)
    linhas, trunc = ctx.sql.consultar(cte + detalhe, params, top)
    for l in linhas:
        if "CODTIPOPER" in l:
            l["NATUREZA"] = ctx.classificacao.natureza_do_pedido(int(l["CODTIPOPER"]))

    por_situacao = {r["SITUACAO_PEDIDO"]: r
                    for r in ctx.sql.consultar(cte + _RESUMO, params, 10)[0]}
    conf = por_situacao.get("confirmado", {})
    resumo = {
        "pedidos": int(conf.get("PEDIDOS") or 0),
        "itens": int(conf.get("ITENS") or 0),
        "vlr_saldo": _num(conf.get("VLR_SALDO")),
        "vlr_atrasado": _num(conf.get("VLR_ATRASADO")),
        "pedidos_atrasados": int(conf.get("PEDIDOS_ATRASADOS") or 0),
        "vlr_em_recebimento": _num(conf.get("VLR_EM_RECEBIMENTO")),
    }
    aguardando, trunc_ag = ctx.sql.consultar(cte + _AGUARDANDO, params, top)
    for l in (*linhas, *aguardando):
        for col in l:
            if col.startswith("VLR_"):
                l[col] = _num(l[col])

    avisos = []
    if int(conf.get("PEDIDOS_SEM_PREVISAO") or 0):
        avisos.append(f"{int(conf['PEDIDOS_SEM_PREVISAO'])} pedidos sem previsão de entrega "
                      "(DTPREVENT vazio): não entram em atrasados nem no_prazo.")
    ag = por_situacao.get("aguardando_autorizacao")
    if ag:
        avisos.append(f"{int(ag['PEDIDOS'])} pedidos aguardando autorização "
                      f"({_reais(ag['VLR_SALDO'])}) estão fora da conta; ver 'aguardando_autorizacao'.")
    nc = por_situacao.get("nao_confirmado")
    if nc:
        avisos.append(f"{int(nc['PEDIDOS'])} pedidos não confirmados e sem pedido de liberação "
                      f"({_reais(nc['VLR_SALDO'])}) estão fora da conta.")
    parado = int(conf.get("PEDIDOS_RECEBIMENTO_PARADO") or 0)
    if parado:
        avisos.append(f"{parado} pedidos com nota de entrada digitada há mais de "
                      f"{_DIAS_RECEBIMENTO_PARADO} dias e ainda não confirmada "
                      "(situacao='em_recebimento', coluna DT_NOTA_RECEBIMENTO).")
    if trunc_ag:
        avisos.append(f"Lista aguardando_autorizacao limitada a {top} pedidos.")
    avisos += _pedidos_nao_classificados(ctx, filtro_emp, p_emp)

    return ctx.politica.resultado(
        linhas, trunc, naturezas=nats, situacao=situacao, agrupar=agrupar, resumo=resumo,
        aguardando_autorizacao=[ctx.politica.limpar_linha(l) for l in aguardando],
        avisos=avisos)


@pack.tool(descricao="""
Lista as naturezas de entrada configuradas (id, descrição, se conta como compra, sinal)
e as TOPs de nota (TOPS) e de pedido (TOPS_PEDIDO) de cada uma. Use para saber quais valores passar em 'naturezas' nas outras tools.
""")
def naturezas_compra(ctx: Contexto) -> dict:
    cl = ctx.classificacao
    linhas = [{"NATUREZA": n.id, "DESCRICAO": n.nome, "CONTA_COMO_COMPRA": n.compra,
               "SINAL": n.sinal, "TOPS": ",".join(str(t) for t in cl.tops_de([n.id])),
               "TOPS_PEDIDO": ",".join(str(t) for t in cl.tops_pedido_de([n.id]))}
              for n in cl.naturezas.values()]
    return ctx.politica.resultado(linhas, False, pendencias_de_classificacao=cl.pendencias)
