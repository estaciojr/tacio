"""Tacio: ponto de entrada. Único arquivo que conhece o SDK MCP; o resto é Python puro e testável."""
import argparse
import functools
import hmac
import inspect
import os
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .adapters.sql_read import LeitorSql
from .audit import Auditoria
from .config import TenantConfig, carregar
from .pack import Contexto, ToolDef, carregar_tools, montar_contexto
from .policy import PoliticaViolada

INSTRUCOES = """Tacio: servidor de dados do ERP Sankhya ({nome}).
Prefira as tools de domínio; use sql_consulta só quando nenhuma outra atender.
Datas no formato AAAA-MM-DD. Valores monetários em reais.
Se a resposta vier com "truncado": true, refine os filtros em vez de concluir com dados parciais."""


def _envolver(td: ToolDef, ctx: Contexto, auditoria: Auditoria):
    """Esconde o parâmetro ctx do agente e adiciona auditoria + tratamento de erro."""
    sig = inspect.signature(td.fn)
    params_publicos = list(sig.parameters.values())[1:]  # remove ctx

    @functools.wraps(td.fn)
    def wrapper(**kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            res = td.fn(ctx, **kwargs)
        except PoliticaViolada as e:
            auditoria.registrar(td.nome, kwargs, ms=(time.perf_counter() - t0) * 1000, erro=str(e))
            raise ToolError(str(e)) from None  # ToolError: mensagem chega ao agente
        except Exception as e:  # erro técnico: registra detalhe, devolve mensagem curta
            auditoria.registrar(td.nome, kwargs, ms=(time.perf_counter() - t0) * 1000,
                                erro=f"{type(e).__name__}: {e}")
            raise ToolError("Falha ao consultar o ERP. Detalhe registrado na auditoria.") from None
        linhas = res.get("total_retornado") if isinstance(res, dict) else None
        auditoria.registrar(td.nome, kwargs, ms=(time.perf_counter() - t0) * 1000, linhas=linhas)
        return res

    del wrapper.__wrapped__  # impede o SDK de ler a assinatura original (com ctx)
    wrapper.__signature__ = sig.replace(parameters=params_publicos)
    wrapper.__annotations__ = {k: v for k, v in td.fn.__annotations__.items() if k != "ctx"}
    return wrapper


def criar_servidor(tenant: TenantConfig, sql: LeitorSql | None = None) -> MCPServer:
    ctx = montar_contexto(tenant, sql)
    auditoria = Auditoria(tenant.auditoria_arquivo, tenant.id)

    srv = MCPServer(name=f"tacio-{tenant.id}", instructions=INSTRUCOES.format(nome=tenant.nome))
    for td in carregar_tools(tenant).values():
        srv.add_tool(
            _envolver(td, ctx, auditoria),
            name=td.nome,
            description=td.descricao,
            annotations=ToolAnnotations(readOnlyHint=td.somente_leitura,
                                        destructiveHint=not td.somente_leitura),
        )
    return srv


class _BearerAuth:
    """Middleware ASGI mínimo: exige Authorization: Bearer <token> no transporte HTTP."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            recebido = headers.get(b"authorization", b"").removeprefix(b"Bearer ").strip()
            if not hmac.compare_digest(recebido, self.token):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"nao autorizado"})
                return
        await self.app(scope, receive, send)


def main() -> None:
    ap = argparse.ArgumentParser(description="Tacio: MCP Server para o Sankhya ERP")
    ap.add_argument("--tenant", required=True, help="Caminho do TOML do tenant")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    args = ap.parse_args()

    tenant = carregar(args.tenant)
    srv = criar_servidor(tenant)

    if args.transport == "stdio":
        srv.run("stdio")
        return

    import uvicorn

    token = os.environ.get(tenant.http.token_env)
    if not token:
        raise SystemExit(f"Defina a variável {tenant.http.token_env} para usar o transporte HTTP.")
    app = srv.streamable_http_app(host=tenant.http.host)
    uvicorn.run(_BearerAuth(app, token), host=tenant.http.host, port=tenant.http.porta)


if __name__ == "__main__":
    main()
