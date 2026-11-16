"""Pack vendas: venda líquida por período, loja, operação, vendedor e produto.

Conceito (o mesmo do pack estoque):
- Universo: nota confirmada (STATUSNOTA 'L') de TOP com GOLSINAL = -1, TGFTOP casada por
  DHTIPOPER. Esse universo traz vendas e devoluções de venda.
- Sinal: GOLSINAL * GOLDEV * -1. +1 soma (venda, estorno de devolução), -1 subtrai (devolução).
- Valor: por nota, VLRNOTA; por item, VLRTOT - VLRDESC (VLRTOT vem antes do desconto).
  O valor por item não inclui frete nem impostos da nota, então não fecha com VLRNOTA.
- Segmentos: recortes do ctx.regra("vendas") (ex.: tenants/dalla.vendas.toml) que ficam fora da
  conta padrão e só entram quando pedidos. Podem ter TOPs fora do universo (GOLSINAL 0), que somam.
- Ano anterior: o mesmo período um ano antes, com as mesmas regras. Em ranking, compara só as
  linhas que entraram no top do período atual.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from ...pack import Contexto, Pack, filtro_in
from ...policy import PoliticaViolada, validar_periodo

pack = Pack("vendas", "Venda líquida por período, loja, operação, vendedor e produto")

_S = "(CASE WHEN TPO.GOLSINAL = -1 THEN TPO.GOLSINAL * TPO.GOLDEV * -1 ELSE 1 END)"
_DE_NOTA = ("FROM sankhya.TGFCAB CAB "
            "JOIN sankhya.TGFTOP TPO ON TPO.CODTIPOPER = CAB.CODTIPOPER AND TPO.DHALTER = CAB.DHTIPOPER")
_DE_ITEM = (f"{_DE_NOTA} "
            "JOIN sankhya.TGFITE ITE ON ITE.NUNOTA = CAB.NUNOTA "
            "JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = ITE.CODPROD")

_MEDIDAS_NOTA = f"""
    SUM(CASE WHEN {_S} > 0 THEN CAB.VLRNOTA ELSE 0 END) AS VLR_VENDA,
    SUM(CASE WHEN {_S} < 0 THEN CAB.VLRNOTA ELSE 0 END) AS VLR_DEVOLUCAO,
    SUM(CAB.VLRNOTA * {_S})                             AS VLR_LIQUIDO,
    SUM(CASE WHEN {_S} > 0 THEN 1 ELSE 0 END)           AS NOTAS_VENDA,
    SUM(CASE WHEN {_S} < 0 THEN 1 ELSE 0 END)           AS NOTAS_DEVOLUCAO"""

_VLR_ITEM = "(ITE.VLRTOT - ISNULL(ITE.VLRDESC, 0))"
_MEDIDAS_ITEM = f"""
    SUM(ITE.QTDNEG * {_S})                                  AS QTD_LIQUIDA,
    SUM(CASE WHEN {_S} > 0 THEN {_VLR_ITEM} ELSE 0 END)     AS VLR_VENDA,
    SUM(CASE WHEN {_S} < 0 THEN {_VLR_ITEM} ELSE 0 END)     AS VLR_DEVOLUCAO,
    SUM({_VLR_ITEM} * {_S})                                 AS VLR_LIQUIDO,
    COUNT(DISTINCT CAB.NUNOTA)                              AS NOTAS"""

_MES = "CONVERT(CHAR(7), CAB.DTNEG, 120)"
_JOIN_EMPRESA = "JOIN sankhya.TSIEMP EMP ON EMP.CODEMP = CAB.CODEMP"

# Modelos com {top} são ranking (corte é esperado); sem {top} são série no tempo (corte é aviso).
_POR_EMPRESA = f"""
    SELECT {{top}} CAB.CODEMP, EMP.NOMEFANTASIA AS EMPRESA, {{medidas}}
    {{de}} {_JOIN_EMPRESA} {{where}}
    GROUP BY CAB.CODEMP, EMP.NOMEFANTASIA ORDER BY VLR_LIQUIDO DESC"""
_POR_MES = f"""
    SELECT {_MES} AS MES, {{medidas}} {{de}} {{where}}
    GROUP BY {_MES} ORDER BY MES"""

_AGRUPAMENTOS_NOTA = {
    "empresa": _POR_EMPRESA,
    "mes": _POR_MES,
    "dia": """
        SELECT CAST(CAB.DTNEG AS DATE) AS DIA, {medidas} {de} {where}
        GROUP BY CAST(CAB.DTNEG AS DATE) ORDER BY DIA""",
    "operacao": """
        SELECT {top} CAB.CODTIPOPER, MAX(TPO.DESCROPER) AS OPERACAO, {medidas} {de} {where}
        GROUP BY CAB.CODTIPOPER ORDER BY VLR_LIQUIDO DESC""",
    "vendedor": """
        SELECT {top} CAB.CODVEND, VEN.APELIDO AS VENDEDOR, {medidas}
        {de} LEFT JOIN sankhya.TGFVEN VEN ON VEN.CODVEND = CAB.CODVEND {where}
        GROUP BY CAB.CODVEND, VEN.APELIDO ORDER BY VLR_LIQUIDO DESC""",
}

_AGRUPAMENTOS_ITEM = {
    "produto": """
        SELECT {top} ITE.CODPROD, PRO.DESCRPROD, {medidas} {de} {where}
        GROUP BY ITE.CODPROD, PRO.DESCRPROD ORDER BY VLR_LIQUIDO DESC""",
    "grupo": """
        SELECT {top} PRO.CODGRUPOPROD, GRU.DESCRGRUPOPROD, {medidas}
        {de} JOIN sankhya.TGFGRU GRU ON GRU.CODGRUPOPROD = PRO.CODGRUPOPROD {where}
        GROUP BY PRO.CODGRUPOPROD, GRU.DESCRGRUPOPROD ORDER BY VLR_LIQUIDO DESC""",
    "vendedor": """
        SELECT {top} ITE.CODVEND, VEN.APELIDO AS VENDEDOR, {medidas}
        {de} LEFT JOIN sankhya.TGFVEN VEN ON VEN.CODVEND = ITE.CODVEND {where}
        GROUP BY ITE.CODVEND, VEN.APELIDO ORDER BY VLR_LIQUIDO DESC""",
    "marca": """
        SELECT {top} RTRIM(PRO.MARCA) AS MARCA, {medidas} {de} {where}
        GROUP BY RTRIM(PRO.MARCA) ORDER BY VLR_LIQUIDO DESC""",
    "empresa": _POR_EMPRESA,
    "mes": _POR_MES,
}

# Coluna que identifica a linha em cada agrupamento: casa o ano anterior com o atual.
# Série no tempo (mes/dia) casa pela data deslocada um ano, sem filtro.
_CHAVE_NOTA = {"empresa": ("CODEMP", "CAB.CODEMP"), "operacao": ("CODTIPOPER", "CAB.CODTIPOPER"),
               "vendedor": ("CODVEND", "CAB.CODVEND"), "mes": ("MES", None), "dia": ("DIA", None)}
_CHAVE_ITEM = {"produto": ("CODPROD", "ITE.CODPROD"), "grupo": ("CODGRUPOPROD", "PRO.CODGRUPOPROD"),
               "vendedor": ("CODVEND", "ITE.CODVEND"), "marca": ("MARCA", "RTRIM(PRO.MARCA)"),
               "empresa": ("CODEMP", "CAB.CODEMP"), "mes": ("MES", None)}


def _num(v) -> float:
    return round(float(v or 0), 2)


@dataclass
class Segmento:
    nome: str
    tops: list[int]
    empresas: list[int]


def _segmentos(ctx: Contexto) -> dict[str, Segmento]:
    segs = {}
    for sid, d in ctx.regra("vendas").get("vendas", {}).get("segmentos", {}).items():
        # TOPs e empresas são interpoladas no SQL: só inteiros passam daqui.
        s = Segmento(d.get("nome", sid), [int(t) for t in d.get("tops", [])],
                     [int(e) for e in d.get("empresas", [])])
        if not s.tops and not s.empresas:
            raise PoliticaViolada(f"Segmento de venda '{sid}' sem tops nem empresas.")
        segs[sid] = s
    return segs


def _ints(valores: list[int]) -> str:
    return ",".join(str(v) for v in sorted(valores))


def _so_do_segmento(s: Segmento) -> str:
    conds = [f"CAB.CODTIPOPER IN ({_ints(s.tops)})"] if s.tops else []
    if s.empresas:
        conds.append(f"(TPO.GOLSINAL = -1 AND CAB.CODEMP IN ({_ints(s.empresas)}))")
    return f"({' OR '.join(conds)})"


def _fora_dos_segmentos(segs: dict[str, Segmento]) -> str:
    cond = "TPO.GOLSINAL = -1"
    tops = {t for s in segs.values() for t in s.tops}
    empresas = {e for s in segs.values() for e in s.empresas}
    if tops:
        cond += f" AND CAB.CODTIPOPER NOT IN ({_ints(tops)})"
    if empresas:
        cond += f" AND CAB.CODEMP NOT IN ({_ints(empresas)})"
    return cond


def _universo(ctx: Contexto, data_inicio: str, data_fim: str, codemp: int | None,
              segmento: str | None) -> tuple[str, list, str, str | None]:
    """WHERE comum, seus parâmetros, o período validado e o segmento efetivo."""
    ini, fim = validar_periodo(data_inicio, data_fim)
    filtro_emp, p_emp = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    segs = _segmentos(ctx)
    if segmento is not None and segmento not in segs:
        raise PoliticaViolada(f"segmento inválido. Válidos: {', '.join(segs) or 'nenhum configurado'}.")
    if segmento is None and codemp is not None:  # perguntar pela empresa é perguntar pelo segmento dela
        segmento = next((sid for sid, s in segs.items() if codemp in s.empresas), None)
    cond = _so_do_segmento(segs[segmento]) if segmento else _fora_dos_segmentos(segs)
    where = (f"WHERE CAB.STATUSNOTA = 'L' AND {cond} "
             f"AND CAB.DTNEG >= ? AND CAB.DTNEG < DATEADD(DAY, 1, ?){filtro_emp}")
    return where, [ini, fim, *p_emp], f"{ini} a {fim}", segmento


def _detalhe(ctx: Contexto, modelo: str, medidas: str, de: str, where: str, params: list,
             top: int | None) -> tuple[list[dict], bool]:
    """top=None tira o corte do ranking (ano anterior, já filtrado pelas chaves do atual)."""
    ranking = "{top}" in modelo and top is not None
    top = max(1, min(top or ctx.max_linhas, ctx.max_linhas))
    sql = modelo.format(top=f"TOP {top}" if ranking else "", medidas=medidas, de=de, where=where)
    linhas, trunc = ctx.sql.consultar(sql, params, top if ranking else ctx.max_linhas)
    for l in linhas:
        for col in l:
            if col.startswith("VLR_"):
                l[col] = _num(l[col])
    return linhas, trunc


def _um_ano_antes(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29/02
        return d.replace(year=d.year - 1, day=28)


def _um_ano_depois(chave):
    """Leva a chave de série do ano anterior para o atual (MES 'AAAA-MM' ou DIA date)."""
    if isinstance(chave, str):
        return f"{int(chave[:4]) + 1}{chave[4:]}"
    if isinstance(chave, date):
        try:
            return chave.replace(year=chave.year + 1)
        except ValueError:
            return chave + timedelta(days=365)
    return chave


def _variacao(atual: float, anterior: float) -> float | None:
    return _num((atual - anterior) / abs(anterior) * 100) if anterior else None


def _comparar_ano_anterior(ctx: Contexto, linhas: list[dict], chave: tuple[str, str | None],
                           modelo: str, medidas: str, de: str, where: str, params: list,
                           ini_ant: date, fim_ant: date, comparadas: list[str]) -> None:
    """Acrescenta <MEDIDA>_ANO_ANTERIOR e VAR_PCT (do VLR_LIQUIDO) a cada linha."""
    col, expr = chave
    params_ant = [ini_ant, fim_ant, *params[2:]]
    where_ant = where
    if expr is not None:
        chaves = [l[col] for l in linhas if l[col] is not None]
        if not chaves:
            return
        where_ant += f" AND {expr} IN ({','.join('?' * len(chaves))})"
        params_ant += chaves
    anteriores, _ = _detalhe(ctx, modelo, medidas, de, where_ant, params_ant, None)
    por_chave = {(l[col] if expr is not None else _um_ano_depois(l[col])): l for l in anteriores}
    for l in linhas:
        ant = por_chave.get(l[col], {})
        for m in comparadas:
            l[f"{m}_ANO_ANTERIOR"] = _num(ant.get(m))
        l["VAR_PCT"] = _variacao(float(l["VLR_LIQUIDO"] or 0), float(ant.get("VLR_LIQUIDO") or 0))


def _resumo_ano_anterior(ctx: Contexto, medidas: str, de: str, where: str, params: list,
                         ini_ant: date, fim_ant: date, resumo: dict, montar) -> dict:
    total = ctx.sql.consultar(f"SELECT {medidas} {de} {where}", [ini_ant, fim_ant, *params[2:]], 1)[0][0]
    ant = montar(total)
    ant["periodo"] = f"{ini_ant} a {fim_ant}"
    ant["var_pct_liquido"] = _variacao(resumo["vlr_liquido"], ant["vlr_liquido"])
    return ant


def _validar_agrupar(agrupar: str, validos: dict) -> str:
    if agrupar not in validos:
        raise PoliticaViolada(f"agrupar inválido. Válidos: {', '.join(validos)}.")
    return validos[agrupar]


@pack.tool(descricao="""
Venda líquida (venda menos devolução) num período, pelo valor da nota. Use para 'quanto vendemos',
'venda por loja', 'por mês', 'por operação', 'ranking de vendedores' e ticket médio.
- agrupar: empresa (padrão) | mes | dia | operacao | vendedor.
- 'resumo' traz o total do período, mesmo quando as linhas são só o top.
- segmento: só quando a pergunta for sobre um dos recortes de segmentos_venda; traz somente ele.
- comparar_ano_anterior=true: o mesmo período um ano antes em cada linha (VLR_*_ANO_ANTERIOR e
  VAR_PCT do líquido) e no resumo (resumo.ano_anterior). Use em vez de chamar duas vezes ou loja a
  loja. Em ranking, compara só as linhas do top atual; por dia, compara a mesma data (não o mesmo
  dia da semana).
Considera a data de negociação (DTNEG) e notas confirmadas.
Datas em AAAA-MM-DD, período máximo de 366 dias. Para produto, grupo ou marca, use vendas_por_produto.
""")
def vendas_resumo(ctx: Contexto, data_inicio: str, data_fim: str, codemp: int | None = None,
                  agrupar: str = "empresa", top: int = 50, segmento: str | None = None,
                  comparar_ano_anterior: bool = False) -> dict:
    modelo = _validar_agrupar(agrupar, _AGRUPAMENTOS_NOTA)
    where, params, periodo, segmento = _universo(ctx, data_inicio, data_fim, codemp, segmento)
    linhas, trunc = _detalhe(ctx, modelo, _MEDIDAS_NOTA, _DE_NOTA, where, params, top)
    for l in linhas:
        l["TICKET_MEDIO"] = _num(l["VLR_VENDA"] / l["NOTAS_VENDA"]) if l["NOTAS_VENDA"] else 0.0

    def montar(total: dict) -> dict:
        return {"vlr_venda": _num(total["VLR_VENDA"]), "vlr_devolucao": _num(total["VLR_DEVOLUCAO"]),
                "vlr_liquido": _num(total["VLR_LIQUIDO"]), "notas_venda": int(total["NOTAS_VENDA"] or 0),
                "notas_devolucao": int(total["NOTAS_DEVOLUCAO"] or 0)}

    resumo = montar(ctx.sql.consultar(f"SELECT {_MEDIDAS_NOTA} {_DE_NOTA} {where}", params, 1)[0][0])
    if comparar_ano_anterior:
        ini_ant, fim_ant = _um_ano_antes(params[0]), _um_ano_antes(params[1])
        _comparar_ano_anterior(ctx, linhas, _CHAVE_NOTA[agrupar], modelo, _MEDIDAS_NOTA, _DE_NOTA, where,
                               params, ini_ant, fim_ant, ["VLR_VENDA", "VLR_LIQUIDO"])
        resumo["ano_anterior"] = _resumo_ano_anterior(ctx, _MEDIDAS_NOTA, _DE_NOTA, where, params,
                                                      ini_ant, fim_ant, resumo, montar)
    return ctx.politica.resultado(linhas, trunc, periodo=periodo, agrupar=agrupar, segmento=segmento,
                                  resumo=resumo)


@pack.tool(descricao="""
Venda líquida por item, descontadas as devoluções: quantidade e valor (VLRTOT - VLRDESC, sem frete
nem impostos da nota). Use para 'mais vendidos', 'quanto vendeu tal produto' ou 'venda por grupo'.
- agrupar: produto (padrão) | grupo | marca | vendedor | empresa | mes.
- Filtros: codprod, codgrupo (código exato do grupo), marca (trecho do nome da marca, ex.: 'DECA';
  agrupe por marca para ver quais marcas o filtro pegou), codvend (vendedor do item), codemp.
- segmento: só quando a pergunta for sobre um dos recortes de segmentos_venda; traz somente ele.
- comparar_ano_anterior=true: o mesmo período um ano antes em cada linha (QTD/VLR_*_ANO_ANTERIOR e
  VAR_PCT do líquido) e no resumo (resumo.ano_anterior). Use em vez de chamar duas vezes. Em
  ranking, compara só as linhas do top atual.
Como não inclui frete nem impostos, o total difere de vendas_resumo.
Datas em AAAA-MM-DD (data de negociação), período máximo de 366 dias.
""")
def vendas_por_produto(ctx: Contexto, data_inicio: str, data_fim: str, codemp: int | None = None,
                       agrupar: str = "produto", codprod: int | None = None,
                       codgrupo: int | None = None, codvend: int | None = None,
                       marca: str | None = None, top: int = 50, segmento: str | None = None,
                       comparar_ano_anterior: bool = False) -> dict:
    modelo = _validar_agrupar(agrupar, _AGRUPAMENTOS_ITEM)
    where, params, periodo, segmento = _universo(ctx, data_inicio, data_fim, codemp, segmento)
    for coluna, valor in (("ITE.CODPROD", codprod), ("PRO.CODGRUPOPROD", codgrupo),
                          ("ITE.CODVEND", codvend)):
        if valor is not None:
            where += f" AND {coluna} = ?"
            params.append(int(valor))
    if marca and marca.strip():
        where += " AND PRO.MARCA LIKE ?"
        params.append(f"%{marca.strip()}%")
    linhas, trunc = _detalhe(ctx, modelo, _MEDIDAS_ITEM, _DE_ITEM, where, params, top)

    def montar(total: dict) -> dict:
        return {"qtd_liquida": _num(total["QTD_LIQUIDA"]), "vlr_venda": _num(total["VLR_VENDA"]),
                "vlr_devolucao": _num(total["VLR_DEVOLUCAO"]), "vlr_liquido": _num(total["VLR_LIQUIDO"]),
                "notas": int(total["NOTAS"] or 0)}

    resumo = montar(ctx.sql.consultar(f"SELECT {_MEDIDAS_ITEM} {_DE_ITEM} {where}", params, 1)[0][0])
    if comparar_ano_anterior:
        ini_ant, fim_ant = _um_ano_antes(params[0]), _um_ano_antes(params[1])
        _comparar_ano_anterior(ctx, linhas, _CHAVE_ITEM[agrupar], modelo, _MEDIDAS_ITEM, _DE_ITEM, where,
                               params, ini_ant, fim_ant, ["QTD_LIQUIDA", "VLR_LIQUIDO"])
        resumo["ano_anterior"] = _resumo_ano_anterior(ctx, _MEDIDAS_ITEM, _DE_ITEM, where, params,
                                                      ini_ant, fim_ant, resumo, montar)
    return ctx.politica.resultado(linhas, trunc, periodo=periodo, agrupar=agrupar, segmento=segmento,
                                  marca=marca, resumo=resumo)


@pack.tool(descricao="""
Lista os segmentos de venda que ficam fora da conta padrão (id, descrição, TOPs e empresas).
Use somente quando a pergunta citar uma empresa ou operação que possa ser um desses recortes, para
descobrir o valor de 'segmento' nas outras tools. Em pergunta genérica de venda, não consulte nem
mencione os segmentos.
""")
def segmentos_venda(ctx: Contexto) -> dict:
    linhas = [{"SEGMENTO": sid, "DESCRICAO": s.nome, "TOPS": _ints(s.tops), "EMPRESAS": _ints(s.empresas)}
              for sid, s in _segmentos(ctx).items()]
    return ctx.politica.resultado(linhas, False)
