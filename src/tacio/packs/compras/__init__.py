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
from ...policy import PoliticaViolada, parse_data

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


def _periodo(data_inicio: str, data_fim: str) -> tuple[date, date]:
    ini, fim = parse_data(data_inicio, "data_inicio"), parse_data(data_fim, "data_fim")
    if ini > fim:
        raise PoliticaViolada("data_inicio é posterior a data_fim.")
    if (fim - ini).days > 366:
        raise PoliticaViolada("Período máximo de 366 dias.")
    return ini, fim


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
    sql = f"""
        SELECT CAB.CODTIPOPER,
               COUNT(*)                    AS QTD_NOTAS,
               SUM(CAB.VLRNOTA)            AS VLR_NOTAS,
               SUM(CASE WHEN GRP.CODPARC IS NOT NULL THEN 1 ELSE 0 END) AS NOTAS_PARC_GRUPO,
               SUM(CASE WHEN {cond_liberado} THEN 1 ELSE 0 END) AS NOTAS_GRUPO_FORNECEDOR
        FROM sankhya.TGFCAB CAB
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
    ini, fim = _periodo(data_inicio, data_fim)
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
    ini, fim = _periodo(data_inicio, data_fim)
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


@pack.tool(descricao="""
Lista as naturezas de entrada configuradas (id, descrição, se conta como compra, sinal)
e as TOPs de cada uma. Use para saber quais valores passar em 'naturezas' nas outras tools.
""")
def naturezas_compra(ctx: Contexto) -> dict:
    cl = ctx.classificacao
    linhas = [{"NATUREZA": n.id, "DESCRICAO": n.nome, "CONTA_COMO_COMPRA": n.compra,
               "SINAL": n.sinal, "TOPS": ",".join(str(t) for t in cl.tops_de([n.id]))}
              for n in cl.naturezas.values()]
    return ctx.politica.resultado(linhas, False, pendencias_de_classificacao=cl.pendencias)
