"""Tacio: ponto de entrada. Só ele e acesso/oauth.py conhecem o SDK MCP; o resto é Python puro e testável."""
import argparse
import functools
import hmac
import inspect
import os
import time
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .acesso.oauth import ESCOPO, ProvedorTacio, registrar_rotas
from .adapters.sql_read import LeitorSql
from .audit import Auditoria
from .config import TenantConfig, carregar
from .pack import Contexto, ToolDef, carregar_tools, montar_contexto
from .policy import PoliticaViolada

INSTRUCOES = """Tacio: servidor de dados do ERP Sankhya ({nome}).
Prefira as tools de domínio; use sql_consulta só quando nenhuma outra atender.
Datas no formato AAAA-MM-DD. Valores monetários em reais.
Se a resposta vier com "truncado": true, refine os filtros em vez de concluir com dados parciais."""


def _usuario_atual() -> str | None:
    """Quem chamou (tenant:USUARIO), quando a chamada veio pelo login OAuth."""
    token = get_access_token()
    return token.subject if token else None


def _envolver(td: ToolDef, ctx: Contexto, auditoria: Auditoria, sql_livre_para: set[str] | None = None):
    """Esconde o parâmetro ctx do agente e adiciona auditoria + tratamento de erro.

    sql_livre_para: com login OAuth, só esses usuários podem usar sql_consulta (None = sem restrição).
    """
    sig = inspect.signature(td.fn)
    params_publicos = list(sig.parameters.values())[1:]  # remove ctx

    @functools.wraps(td.fn)
    def wrapper(**kwargs: Any) -> Any:
        t0 = time.perf_counter()
        usuario = _usuario_atual()
        try:
            if td.nome == "sql_consulta" and sql_livre_para is not None:
                if (usuario or "").split(":", 1)[-1] not in sql_livre_para:
                    raise PoliticaViolada("Consulta SQL livre não está liberada para o seu usuário. "
                                          "Use as tools de domínio.")
            res = td.fn(ctx, **kwargs)
        except PoliticaViolada as e:
            auditoria.registrar(td.nome, kwargs, ms=(time.perf_counter() - t0) * 1000, erro=str(e),
                                usuario=usuario)
            raise ToolError(str(e)) from None  # ToolError: mensagem chega ao agente
        except Exception as e:  # erro técnico: registra detalhe, devolve mensagem curta
            auditoria.registrar(td.nome, kwargs, ms=(time.perf_counter() - t0) * 1000,
                                erro=f"{type(e).__name__}: {e}", usuario=usuario)
            raise ToolError("Falha ao consultar o ERP. Detalhe registrado na auditoria.") from None
        linhas = res.get("total_retornado") if isinstance(res, dict) else None
        auditoria.registrar(td.nome, kwargs, ms=(time.perf_counter() - t0) * 1000, linhas=linhas,
                            usuario=usuario)
        return res

    del wrapper.__wrapped__  # impede o SDK de ler a assinatura original (com ctx)
    wrapper.__signature__ = sig.replace(parameters=params_publicos)
    wrapper.__annotations__ = {k: v for k, v in td.fn.__annotations__.items() if k != "ctx"}
    return wrapper


def criar_servidor(tenant: TenantConfig, sql: LeitorSql | None = None,
                   provedor: ProvedorTacio | None = None) -> MCPServer:
    ctx = montar_contexto(tenant, sql)
    auditoria = Auditoria(tenant.auditoria_arquivo, tenant.id)

    auth: dict[str, Any] = {}
    sql_livre_para = None
    if provedor is not None:
        auth = {"auth_server_provider": provedor, "auth": AuthSettings(
            issuer_url=provedor.url, resource_server_url=provedor.recurso, validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[ESCOPO], default_scopes=[ESCOPO]),
            revocation_options=RevocationOptions(enabled=True))}
        sql_livre_para = {u.upper() for u in tenant.oauth.usuarios_sql_livre}

    srv = MCPServer(name=f"tacio-{tenant.id}", instructions=INSTRUCOES.format(nome=tenant.nome), **auth)
    if provedor is not None:
        registrar_rotas(srv, provedor)
    for td in carregar_tools(tenant).values():
        srv.add_tool(
            _envolver(td, ctx, auditoria, sql_livre_para),
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


def app_oauth(tenant: TenantConfig, provedor: ProvedorTacio | None = None, sql: LeitorSql | None = None):
    """App HTTP com login OAuth: cada usuário entra com a própria senha do Sankhya."""
    from urllib.parse import urlparse

    from .acesso.armazem import Armazem
    from .acesso.sankhya_login import LoginSankhya

    cfg = tenant.oauth
    if provedor is None:
        provedor = ProvedorTacio(cfg, tenant.id, LoginSankhya(cfg.sankhya_url), Armazem(cfg.banco))
    srv = criar_servidor(tenant, sql, provedor=provedor)
    publico = urlparse(provedor.url)
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", publico.netloc, f"{publico.hostname}:*"]
    origens = ["http://127.0.0.1:*", "http://localhost:*", f"{publico.scheme}://{publico.netloc}"]
    return srv.streamable_http_app(host=tenant.http.host, transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origens))


def main() -> None:
    ap = argparse.ArgumentParser(description="Tacio: MCP Server para o Sankhya ERP")
    ap.add_argument("--tenant", required=True, help="Caminho do TOML do tenant")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    args = ap.parse_args()

    tenant = carregar(args.tenant)
    if args.transport == "stdio":
        criar_servidor(tenant).run("stdio")
        return

    import uvicorn

    if tenant.oauth is not None:
        uvicorn.run(app_oauth(tenant), host=tenant.http.host, port=tenant.http.porta)
        return

    srv = criar_servidor(tenant)
    token = os.environ.get(tenant.http.token_env)
    if not token:
        raise SystemExit(f"Defina a variável {tenant.http.token_env} para usar o transporte HTTP.")
    app = srv.streamable_http_app(host=tenant.http.host)
    uvicorn.run(_BearerAuth(app, token), host=tenant.http.host, port=tenant.http.porta)


if __name__ == "__main__":
    main()
