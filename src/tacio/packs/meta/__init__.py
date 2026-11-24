"""Pack meta: dicionário de dados do Sankhya e consulta SQL livre (com guarda)."""
import difflib
import re

import sqlglot
from sqlglot import exp

from ...adapters.sql_read import validar_somente_leitura
from ...pack import Contexto, Pack
from ...policy import PoliticaViolada

pack = Pack("meta", "Dicionário de dados e consulta livre")

_COLUNA_INVALIDA = re.compile(r"Invalid column name '([^']+)'", re.IGNORECASE)
_MSG_SQLSERVER = re.compile(r"\[SQL Server\](.*?)(?:\s*\(\d+\)|\n|$)", re.DOTALL)
# Armadilhas de T-SQL que o agente repete: (trecho da mensagem do SQL Server, orientação).
_DICAS_TSQL = [
    ("text, ntext, and image", "Coluna text/ntext não entra em DISTINCT, GROUP BY, UNION nem comparação: "
                               "use CAST(coluna AS varchar(max))."),
    ("Operand data type", "Tipos incompatíveis na operação: converta com CAST/CONVERT "
                          "(data como texto: CONVERT(varchar(10), data, 23))."),
    ("Argument data type", "Tipo incompatível com a função: converta com CAST/CONVERT antes de aplicá-la."),
    ("aggregate function on an expression containing an aggregate",
     "Não dá para agregar um agregado ou subconsulta direto: calcule o interno em CTE ou subconsulta "
     "e agregue por fora."),
    ("Query timeout expired", "A consulta passou do tempo limite: reduza o período, filtre por empresa "
                              "e agregue no banco."),
]


def _colunas_parecidas(ctx: Contexto, sql: str, ruim: str) -> list[str]:
    """Colunas das tabelas da consulta (dicionário TDDCAM) com nome parecido ao inválido."""
    try:
        tabelas = sorted({t.name.upper() for s in sqlglot.parse(sql, read="tsql") if s is not None
                          for t in s.find_all(exp.Table) if t.name})[:8]
        if not tabelas:
            return []
        marcas = ",".join("?" * len(tabelas))
        linhas, _ = ctx.sql.consultar(
            f"SELECT NOMETAB, NOMECAMPO FROM sankhya.TDDCAM WHERE NOMETAB IN ({marcas})",
            tabelas, 5000)
    except Exception:  # sugestão é bônus: nunca esconde o erro original
        return []
    alvo = ruim.upper()
    por_nome: dict[str, list[str]] = {}
    for l in linhas:
        por_nome.setdefault(str(l["NOMECAMPO"]).strip().upper(), []).append(str(l["NOMETAB"]).strip().upper())
    nomes = list(por_nome)
    achados = difflib.get_close_matches(alvo, nomes, n=5, cutoff=0.6)
    achados += [n for n in nomes if n not in achados and len(alvo) >= 4 and (alvo in n or n in alvo)][:5]
    return [f"{t}.{n}" for n in achados[:6] for t in por_nome[n][:2]]


def _explicar_erro(ctx: Contexto, sql: str, e: Exception) -> None:
    """Troca erro técnico do SQL Server por orientação que o agente consegue usar. Levanta
    PoliticaViolada se souber explicar; senão retorna e o chamador relança o original."""
    bruto = str(e)
    m = _COLUNA_INVALIDA.search(bruto)
    if m:
        ruim = m.group(1)
        parecidas = _colunas_parecidas(ctx, sql, ruim)
        msg = f"A coluna '{ruim}' não existe nas tabelas desta consulta."
        if parecidas:
            msg += f" Parecidas: {', '.join(parecidas)}."
        raise PoliticaViolada(msg + " Confira os nomes com descrever_tabela ou buscar_campo antes de repetir.")
    for trecho, dica in _DICAS_TSQL:
        if trecho.lower() in bruto.lower():
            detalhe = _MSG_SQLSERVER.search(bruto)
            resumo = f" (SQL Server: {detalhe.group(1).strip()[:160]})" if detalhe else ""
            raise PoliticaViolada(dica + resumo)


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
Em tabela que você não conhece, rode descrever_tabela antes: chutar nome de coluna é o erro mais comum.
Para achar produto ou grupo por nome, use as tools produto e grupos_produto em vez de SQL.
""")
def sql_consulta(ctx: Contexto, sql: str) -> dict:
    cfg = ctx.politica.cfg
    if not cfg.sql_livre:
        raise PoliticaViolada("Consulta SQL livre está desativada neste servidor.")
    validar_somente_leitura(sql, cfg.tabelas_sql_livre)
    try:
        linhas, trunc = ctx.sql.consultar(sql, (), ctx.max_linhas)
    except PoliticaViolada:
        raise
    except Exception as e:
        _explicar_erro(ctx, sql, e)
        raise
    return ctx.politica.resultado(linhas, trunc)
