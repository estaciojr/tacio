"""Pack meta: dicionário de dados do Sankhya e consulta SQL livre (com guarda)."""
from ...adapters.sql_read import validar_somente_leitura
from ...pack import Contexto, Pack
from ...policy import PoliticaViolada

pack = Pack("meta", "Dicionário de dados e consulta livre")


@pack.tool(descricao="""
Descreve as colunas de uma tabela do Sankhya a partir do dicionário de dados (TDDCAM),
incluindo campos customizados AD_. Use antes de montar uma consulta em tabela desconhecida.
""")
def descrever_tabela(ctx: Contexto, tabela: str) -> dict:
    sql = """
        SELECT C.NOMECAMPO, C.DESCRCAMPO, C.TIPCAMPO
        FROM sankhya.TDDCAM C
        WHERE C.NOMETAB = ?
        ORDER BY C.NOMECAMPO
    """
    linhas, trunc = ctx.sql.consultar(sql, [tabela.strip().upper()], max_linhas=500)
    if not linhas:
        raise PoliticaViolada(f"Tabela '{tabela}' não encontrada no dicionário.")
    return ctx.politica.resultado(linhas, trunc, tabela=tabela.upper())


@pack.tool(descricao="""
Procura campos no dicionário de dados pelo nome ou pela descrição (ex.: 'margem', 'AD_LOJA').
Retorna tabela, campo e descrição.
""")
def buscar_campo(ctx: Contexto, termo: str) -> dict:
    sql = """
        SELECT TOP 100 C.NOMETAB, C.NOMECAMPO, C.DESCRCAMPO
        FROM sankhya.TDDCAM C
        WHERE C.NOMECAMPO LIKE ? OR C.DESCRCAMPO LIKE ?
        ORDER BY C.NOMETAB, C.NOMECAMPO
    """
    padrao = f"%{termo.strip()}%"
    linhas, trunc = ctx.sql.consultar(sql, [padrao, padrao], ctx.max_linhas)
    return ctx.politica.resultado(linhas, trunc, termo=termo)


@pack.tool(descricao="""
Executa um SELECT livre no banco do Sankhya (T-SQL). Use SOMENTE quando nenhuma outra
tool atender. Apenas um SELECT, sem escrita, restrito às tabelas liberadas.
Qualifique sempre as tabelas com o schema sankhya (ex.: sankhya.TGFCAB).
Prefira agregar (SUM, COUNT, GROUP BY) a trazer linhas soltas.
""")
def sql_consulta(ctx: Contexto, sql: str) -> dict:
    cfg = ctx.politica.cfg
    if not cfg.sql_livre:
        raise PoliticaViolada("Consulta SQL livre está desativada neste servidor.")
    validar_somente_leitura(sql, cfg.tabelas_sql_livre)
    linhas, trunc = ctx.sql.consultar(sql, (), ctx.max_linhas)
    return ctx.politica.resultado(linhas, trunc)
