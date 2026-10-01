"""Pack estoque: ruptura, risco de ruptura e posição de estoque.

Regra padrão (validar contra o DS 561 antes de produção):
- Venda: TGFCAB TIPMOV 'V' + STATUSNOTA 'L' no período.
- Estoque disponível: SUM(ESTOQUE - RESERVADO) da TGFEST, estoque próprio (TIPO 'P').
- Ruptura: produto que VENDEU no período e está com disponível <= 0 na empresa.
- Risco: disponível > 0, mas cobertura (disponível / venda média diária) abaixo do mínimo.
"""
from datetime import date, timedelta

from ...pack import Contexto, Pack, filtro_in
from ...policy import PoliticaViolada

pack = Pack("estoque", "Ruptura e posição de estoque")

_VENDA_CONFIRMADA = "CAB.TIPMOV = 'V' AND CAB.STATUSNOTA = 'L'"
_ESTOQUE_PROPRIO = "EST.TIPO = 'P'"


@pack.tool(descricao="""
Produtos em ruptura (vendeu no período e está sem estoque disponível na loja) e, opcionalmente,
em risco de ruptura (cobertura em dias abaixo do mínimo). Ordenado por status e pelo valor
vendido no período, que indica o impacto da falta. Use para 'o que está faltando nas lojas'.
""")
def ruptura_por_loja(ctx: Contexto, dias: int = 30, codemp: int | None = None,
                     incluir_risco: bool = True, cobertura_min_dias: int = 7,
                     top: int = 100) -> dict:
    if not 7 <= dias <= 180:
        raise PoliticaViolada("dias deve estar entre 7 e 180.")
    if not 1 <= cobertura_min_dias <= 90:
        raise PoliticaViolada("cobertura_min_dias deve estar entre 1 e 90.")
    top = max(1, min(int(top), ctx.max_linhas))
    empresas = ctx.politica.validar_codemp(codemp)
    f_cab, p_cab = filtro_in("CAB.CODEMP", empresas)
    f_est, p_est = filtro_in("EST.CODEMP", empresas)
    desde = date.today() - timedelta(days=dias)
    condicao_risco = (
        f"OR (E.DISPONIVEL > 0 AND E.DISPONIVEL / (V.QTD * 1.0 / {dias}) < {cobertura_min_dias})"
        if incluir_risco else ""
    )
    # dias, cobertura_min_dias e top são inteiros validados acima; o resto é parametrizado.
    sql = f"""
        WITH VENDAS AS (
            SELECT CAB.CODEMP, ITE.CODPROD,
                   SUM(ITE.QTDNEG) AS QTD, SUM(ITE.VLRTOT) AS VLR
            FROM sankhya.TGFCAB CAB
            JOIN sankhya.TGFITE ITE ON ITE.NUNOTA = CAB.NUNOTA
            WHERE {_VENDA_CONFIRMADA} AND CAB.DTNEG >= ?{f_cab}
            GROUP BY CAB.CODEMP, ITE.CODPROD
            HAVING SUM(ITE.QTDNEG) > 0
        ),
        ESTQ AS (
            SELECT EST.CODEMP, EST.CODPROD, SUM(EST.ESTOQUE - EST.RESERVADO) AS DISPONIVEL
            FROM sankhya.TGFEST EST
            WHERE {_ESTOQUE_PROPRIO}{f_est}
            GROUP BY EST.CODEMP, EST.CODPROD
        ),
        BASE AS (
            SELECT V.CODEMP, V.CODPROD, PRO.DESCRPROD,
                   COALESCE(E.DISPONIVEL, 0) AS DISPONIVEL,
                   CAST(V.QTD * 1.0 / {dias} AS DECIMAL(18, 2)) AS VENDA_MEDIA_DIA,
                   CASE WHEN COALESCE(E.DISPONIVEL, 0) <= 0 THEN 0
                        ELSE CAST(E.DISPONIVEL / (V.QTD * 1.0 / {dias}) AS DECIMAL(18, 1))
                   END AS COBERTURA_DIAS,
                   CASE WHEN COALESCE(E.DISPONIVEL, 0) <= 0 THEN 'RUPTURA' ELSE 'RISCO' END AS STATUS,
                   V.VLR AS VLR_VENDIDO_PERIODO
            FROM VENDAS V
            JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = V.CODPROD
            LEFT JOIN ESTQ E ON E.CODEMP = V.CODEMP AND E.CODPROD = V.CODPROD
            WHERE COALESCE(E.DISPONIVEL, 0) <= 0 {condicao_risco}
        )
        SELECT TOP {top} *
        FROM BASE
        ORDER BY CASE STATUS WHEN 'RUPTURA' THEN 0 ELSE 1 END, VLR_VENDIDO_PERIODO DESC
    """
    linhas, trunc = ctx.sql.consultar(sql, [desde, *p_cab, *p_est], ctx.max_linhas)
    rupturas = sum(1 for l in linhas if l.get("STATUS") == "RUPTURA")
    return ctx.politica.resultado(
        linhas, trunc or len(linhas) >= top,
        periodo_dias=dias, rupturas=rupturas, riscos=len(linhas) - rupturas,
    )


@pack.tool(descricao="""
Posição de estoque de um produto por empresa: estoque, reservado e disponível.
Use para 'tem X em estoque?' ou 'em qual loja tem X'.
""")
def estoque_produto(ctx: Contexto, codprod: int, codemp: int | None = None) -> dict:
    f_est, p_est = filtro_in("EST.CODEMP", ctx.politica.validar_codemp(codemp))
    sql = f"""
        SELECT EST.CODEMP, PRO.CODPROD, PRO.DESCRPROD,
               SUM(EST.ESTOQUE) AS ESTOQUE,
               SUM(EST.RESERVADO) AS RESERVADO,
               SUM(EST.ESTOQUE - EST.RESERVADO) AS DISPONIVEL
        FROM sankhya.TGFEST EST
        JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = EST.CODPROD
        WHERE {_ESTOQUE_PROPRIO} AND EST.CODPROD = ?{f_est}
        GROUP BY EST.CODEMP, PRO.CODPROD, PRO.DESCRPROD
        ORDER BY EST.CODEMP
    """
    linhas, trunc = ctx.sql.consultar(sql, [codprod, *p_est], ctx.max_linhas)
    return ctx.politica.resultado(linhas, trunc, codprod=codprod)
