"""Leitura no SQL Server do Sankhya. Somente SELECT, sempre com timeout e limite de linhas."""
import re
from typing import Any, Protocol, Sequence

import sqlglot
from sqlglot import exp

from ..config import SqlConfig, ler_variavel
from ..policy import PoliticaViolada

_PROIBIDOS = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
    exp.Alter, exp.Command, exp.Into, exp.TruncateTable,
)
# "sankhya." qualificando tabela; não pega nome de pacote como 'br.com.sankhya.x'.
_SCHEMA_RE = re.compile(r"(?<![\w.])sankhya\.(?=[A-Za-z_])", re.IGNORECASE)


def validar_somente_leitura(sql: str, tabelas_permitidas: Sequence[str] | None = None) -> None:
    """Rejeita qualquer coisa que não seja um único SELECT (CTE permitida).

    Se tabelas_permitidas for informado, toda tabela referenciada precisa estar na lista.
    """
    try:
        stmts = [s for s in sqlglot.parse(sql, read="tsql") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise PoliticaViolada(f"SQL inválido: {e}") from e
    if len(stmts) != 1:
        raise PoliticaViolada("Envie exatamente um comando SELECT.")
    stmt = stmts[0]
    if not isinstance(stmt, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise PoliticaViolada("Somente SELECT é permitido.")
    if any(stmt.find_all(*_PROIBIDOS)):
        raise PoliticaViolada("Comando de escrita/DDL detectado; somente leitura é permitida.")
    if tabelas_permitidas is not None and "*" not in tabelas_permitidas:
        permitidas = {t.upper() for t in tabelas_permitidas}
        ctes = {c.alias_or_name.upper() for c in stmt.find_all(exp.CTE)}
        usadas = {t.name.upper() for t in stmt.find_all(exp.Table)} - ctes
        fora = sorted(usadas - permitidas)
        if fora:
            raise PoliticaViolada(f"Tabelas não liberadas para consulta livre: {', '.join(fora)}")


def aplicar_schema(sql: str, schema: str) -> str:
    """Troca o schema das tabelas: os packs escrevem sempre sankhya.TABELA."""
    if schema.lower() == "sankhya":
        return sql
    return _SCHEMA_RE.sub(f"{schema}.", sql)


class LeitorSql(Protocol):
    def consultar(self, sql: str, params: Sequence[Any] = (), max_linhas: int = 200
                  ) -> tuple[list[dict[str, Any]], bool]: ...


class SqlServerLeitor:
    """Implementação real via pyodbc. Conexão por chamada (o pooling do ODBC cuida do custo)."""

    def __init__(self, cfg: SqlConfig):
        self.cfg = cfg

    def _conectar(self):
        import pyodbc  # import tardio: testes rodam sem driver ODBC

        if self.cfg.senha_env and not ler_variavel(self.cfg.senha_env):
            raise PoliticaViolada(f"Senha do banco não configurada: defina a variável de ambiente "
                                  f"{self.cfg.senha_env} e reinicie o cliente.")

        conn = pyodbc.connect(self.cfg.connection_string(), autocommit=True, readonly=True)
        conn.timeout = self.cfg.timeout_s
        if self.cfg.leitura_suja:
            conn.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        return conn

    def consultar(self, sql: str, params: Sequence[Any] = (), max_linhas: int = 200
                  ) -> tuple[list[dict[str, Any]], bool]:
        validar_somente_leitura(sql)
        with self._conectar() as conn:
            cur = conn.cursor()
            cur.execute(aplicar_schema(sql, self.cfg.schema), *params)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchmany(max_linhas + 1)
        truncado = len(rows) > max_linhas
        return [dict(zip(cols, r)) for r in rows[:max_linhas]], truncado
