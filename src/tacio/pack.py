"""Contrato dos packs.

Um pack é um módulo em tacio.packs.<nome> que expõe `pack = Pack(...)` e
decora funções com @pack.tool(). Toda função recebe `ctx: Contexto` como primeiro
parâmetro; o restante vira o schema da tool que o agente enxerga.

    pack = Pack("compras", "Análise de compras e fornecedores")

    @pack.tool(descricao="...")
    def ultima_compra(ctx: Contexto, codprod: int) -> dict:
        linhas, trunc = ctx.sql.consultar("SELECT ...", [codprod], ctx.max_linhas)
        return ctx.politica.resultado(linhas, trunc)
"""
import importlib
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable

from .adapters.sql_read import LeitorSql, SqlServerLeitor
from .classificacao import Classificacao, carregar_classificacao
from .config import TenantConfig
from .policy import Politica, PoliticaViolada


@dataclass
class Contexto:
    tenant: TenantConfig
    sql: LeitorSql
    politica: Politica
    classificacao_: Classificacao | None = None
    _regras: dict[str, dict] = field(default_factory=dict)

    @property
    def max_linhas(self) -> int:
        return self.politica.cfg.max_linhas

    @property
    def classificacao(self) -> Classificacao:
        if self.classificacao_ is None:
            raise PoliticaViolada("Classificação de TOPs não configurada para este cliente.")
        return self.classificacao_

    def regra(self, nome: str) -> dict:
        """Parâmetros de regra do tenant (TOML em [regras]); lidos uma vez e guardados."""
        if nome not in self._regras:
            arquivo = self.tenant.regras.get(nome)
            if not arquivo:
                raise PoliticaViolada(f"Regra '{nome}' não configurada para este cliente.")
            with open(arquivo, "rb") as f:
                self._regras[nome] = tomllib.load(f)
        return self._regras[nome]


@dataclass
class ToolDef:
    nome: str
    descricao: str
    fn: Callable[..., Any]
    somente_leitura: bool = True
    pack: str = ""


@dataclass
class Pack:
    nome: str
    descricao: str
    tools: list[ToolDef] = field(default_factory=list)

    def tool(self, descricao: str, nome: str | None = None, somente_leitura: bool = True):
        def deco(fn: Callable[..., Any]):
            self.tools.append(ToolDef(nome or fn.__name__, descricao.strip(), fn, somente_leitura, self.nome))
            return fn
        return deco


def carregar_pack(nome: str) -> Pack:
    mod = importlib.import_module(f"tacio.packs.{nome}")
    pack = getattr(mod, "pack", None)
    if not isinstance(pack, Pack):
        raise RuntimeError(f"Módulo tacio.packs.{nome} não expõe `pack = Pack(...)`.")
    return pack


# --- utilitários para escrever tools ---------------------------------------
def filtro_in(coluna: str, valores: list[int] | None) -> tuple[str, list[int]]:
    """Gera ' AND COL IN (?,?)' parametrizado; string vazia se não houver filtro."""
    if not valores:
        return "", []
    return f" AND {coluna} IN ({','.join('?' * len(valores))})", list(valores)


# --- montagem compartilhada (servidor MCP e operators usam o mesmo caminho) ------
def montar_contexto(tenant: TenantConfig, sql: LeitorSql | None = None) -> Contexto:
    classificacao = (carregar_classificacao(tenant.classificacao_tops)
                     if tenant.classificacao_tops else None)
    return Contexto(tenant=tenant, sql=sql or SqlServerLeitor(tenant.sql),
                    politica=Politica(tenant.politica, tenant.codemp_permitidas),
                    classificacao_=classificacao)


def carregar_tools(tenant: TenantConfig) -> dict[str, ToolDef]:
    tools: dict[str, ToolDef] = {}
    for nome_pack in tenant.packs:
        for td in carregar_pack(nome_pack).tools:
            if td.nome in tools:
                raise RuntimeError(f"Tool '{td.nome}' registrada por mais de um pack.")
            tools[td.nome] = td
    return tools
