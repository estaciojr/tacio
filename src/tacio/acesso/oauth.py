"""Servidor de autorização OAuth do Tacio (o SDK MCP cuida do protocolo; aqui fica o "quem é").

Fluxo: o assistente (Claude, ChatGPT) registra-se sozinho, manda o usuário para /authorize,
o SDK chama `authorize()`, que guarda o pedido e devolve a URL da tela /login. A tela pede
usuário e senha (e o CNPJ, se o tenant listar CNPJs), confere a senha no Sankhya (sankhya_login) e devolve um código ao
assistente, que o troca por tokens em /token. A senha e a sessão do Sankhya não são guardadas.
"""
import asyncio
import html
import json
import re
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      RefreshToken, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from ..config import OAuthConfig
from .armazem import Armazem, hash_segredo
from .sankhya_login import ErroSankhya, LoginSankhya, UsuarioSankhya

ESCOPO = "tacio:consulta"
TTL_PEDIDO_S = 600
TTL_CODIGO_S = 300
MAX_FALHAS = 5
JANELA_FALHAS_S = 15 * 60


class CodigoTacio(AuthorizationCode):
    codusu: int | None = None


class RefreshTacio(RefreshToken):
    codusu: int | None = None
    acesso_hash: str | None = None


class AcessoTacio(AccessToken):
    refresh_hash: str | None = None


def so_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto or "")


class ProvedorTacio:
    """Implementa o OAuthAuthorizationServerProvider do SDK sobre o Armazem."""

    def __init__(self, cfg: OAuthConfig, tenant_id: str, login: LoginSankhya, armazem: Armazem):
        self.cfg = cfg
        self.tenant_id = tenant_id
        self.login = login
        self.armazem = armazem
        self.url = cfg.url_publica.rstrip("/")
        self.recurso = f"{self.url}/mcp"
        self.cnpjs = {so_digitos(c) for c in cfg.cnpjs}
        self.permitidos = {u.upper() for u in (cfg.usuarios or cfg.usuarios_permitidos)}
        self._falhas: dict[str, deque] = defaultdict(deque)

    # ---- clientes (registro dinâmico: o Claude/ChatGPT se registra sozinho)
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        valor = self.armazem.ler("cliente", client_id)
        return OAuthClientInformationFull.model_validate_json(valor) if valor else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.armazem.gravar("cliente", client_info.client_id, client_info.model_dump_json(), None)

    # ---- /authorize: guarda o pedido e manda para a tela de login
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pedido = secrets.token_urlsafe(32)
        self.armazem.gravar("pedido", pedido, json.dumps({
            "client_id": client.client_id, "params": params.model_dump(mode="json")}),
            time.time() + TTL_PEDIDO_S)
        return f"{self.url}/login?pedido={pedido}"

    def ler_pedido(self, pedido: str) -> tuple[OAuthClientInformationFull | None, AuthorizationParams] | None:
        valor = self.armazem.ler("pedido", pedido) if pedido else None
        if not valor:
            return None
        dados = json.loads(valor)
        cliente = self.armazem.ler("cliente", dados["client_id"])
        info = OAuthClientInformationFull.model_validate_json(cliente) if cliente else None
        return info, AuthorizationParams.model_validate(dados["params"])

    # ---- tela de login: decide se o usuário entra
    def bloqueado(self, usuario: str) -> bool:
        fila, agora = self._falhas[usuario.upper()], time.time()
        while fila and fila[0] < agora - JANELA_FALHAS_S:
            fila.popleft()
        return len(fila) >= MAX_FALHAS

    def _falhou(self, usuario: str) -> None:
        self._falhas[usuario.upper()].append(time.time())

    async def entrar(self, pedido: str, cnpj: str, usuario: str, senha: str) -> tuple[str | None, str | None]:
        """Devolve (url de retorno ao assistente, None) ou (None, mensagem de erro para a tela)."""
        lido = self.ler_pedido(pedido)
        if lido is None:
            return None, "Este pedido de acesso expirou. Volte ao assistente e conecte de novo."
        cliente, params = lido
        if cliente is None:
            return None, "Aplicativo não reconhecido. Volte ao assistente e conecte de novo."
        if self.cnpjs and so_digitos(cnpj) not in self.cnpjs:
            return None, "CNPJ não habilitado para o Tacio."
        if self.bloqueado(usuario):
            return None, "Muitas tentativas com este usuário. Aguarde 15 minutos."
        try:
            quem: UsuarioSankhya | None = await asyncio.to_thread(self.login.validar, usuario, senha)
        except ErroSankhya:
            return None, "Não foi possível falar com o Sankhya agora. Tente de novo em instantes."
        if quem is None:
            self._falhou(usuario)
            return None, "Usuário ou senha inválidos."
        if self.permitidos and quem.nome not in self.permitidos:
            return None, "Seu usuário ainda não foi liberado para o Tacio. Fale com o TI."

        codigo = secrets.token_urlsafe(32)
        self.armazem.apagar("pedido", pedido)
        self.armazem.gravar("codigo", hash_segredo(codigo), CodigoTacio(
            code=hash_segredo(codigo), scopes=params.scopes or [ESCOPO], expires_at=time.time() + TTL_CODIGO_S,
            client_id=cliente.client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource, subject=f"{self.tenant_id}:{quem.nome}", codusu=quem.codusu,
        ).model_dump_json(), time.time() + TTL_CODIGO_S)
        return construct_redirect_uri(str(params.redirect_uri), code=codigo, state=params.state), None

    # ---- /token: código -> tokens
    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> CodigoTacio | None:
        valor = self.armazem.ler("codigo", hash_segredo(authorization_code))
        if not valor:
            return None
        codigo = CodigoTacio.model_validate_json(valor)
        return codigo if codigo.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: CodigoTacio) -> OAuthToken:
        self.armazem.apagar("codigo", authorization_code.code)  # uso único (code guarda o hash)
        return self._emitir(client.client_id, authorization_code.scopes, authorization_code.subject,
                            authorization_code.codusu, authorization_code.resource)

    def _emitir(self, client_id: str, escopos: list[str], sujeito: str | None,
                codusu: int | None, recurso: str | None) -> OAuthToken:
        acesso, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        h_acesso, h_refresh = hash_segredo(acesso), hash_segredo(refresh)
        agora = int(time.time())
        vence_acesso = agora + self.cfg.ttl_acesso_s
        vence_refresh = agora + self.cfg.ttl_refresh_dias * 86400
        recurso = recurso or self.recurso
        self.armazem.gravar("acesso", h_acesso, AcessoTacio(
            token=h_acesso, client_id=client_id, scopes=escopos, expires_at=vence_acesso,
            resource=recurso, subject=sujeito, claims={"iss": self.url, "codusu": codusu},
            refresh_hash=h_refresh).model_dump_json(), vence_acesso)
        self.armazem.gravar("refresh", h_refresh, RefreshTacio(
            token=h_refresh, client_id=client_id, scopes=escopos, expires_at=vence_refresh,
            resource=recurso, subject=sujeito, codusu=codusu, acesso_hash=h_acesso,
        ).model_dump_json(), vence_refresh)
        return OAuthToken(access_token=acesso, token_type="Bearer", expires_in=self.cfg.ttl_acesso_s,
                          refresh_token=refresh, scope=" ".join(escopos))

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshTacio | None:
        valor = self.armazem.ler("refresh", hash_segredo(refresh_token))
        if not valor:
            return None
        rt = RefreshTacio.model_validate_json(valor)
        return rt if rt.client_id == client.client_id else None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshTacio, scopes: list[str]) -> OAuthToken:
        if self.permitidos and (refresh_token.subject or "").split(":", 1)[-1] not in self.permitidos:
            raise TokenError("invalid_grant", "usuário não está mais liberado")
        await self.revoke_token(refresh_token)  # rotação: o refresh antigo não vale mais
        return self._emitir(client.client_id, scopes or refresh_token.scopes, refresh_token.subject,
                            refresh_token.codusu, refresh_token.resource)

    # ---- cada chamada ao /mcp
    async def load_access_token(self, token: str) -> AcessoTacio | None:
        valor = self.armazem.ler("acesso", hash_segredo(token))
        if not valor:
            return None
        acesso = AcessoTacio.model_validate_json(valor)
        if self.permitidos and (acesso.subject or "").split(":", 1)[-1] not in self.permitidos:
            return None  # tirar o usuário da lista corta o acesso na hora
        return acesso

    async def revoke_token(self, token: AcessoTacio | RefreshTacio) -> None:
        if isinstance(token, RefreshTacio):
            self.armazem.apagar("refresh", token.token)
            if token.acesso_hash:
                self.armazem.apagar("acesso", token.acesso_hash)
        else:
            self.armazem.apagar("acesso", token.token)
            if getattr(token, "refresh_hash", None):
                self.armazem.apagar("refresh", token.refresh_hash)


# ---------------------------------------------------------------- tela de login

_PAGINA = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tacio — Autorizar acesso</title>
<style>
:root{{--fundo:#f4f6f5;--cartao:#fff;--texto:#16201c;--suave:#5b6b64;--borda:#d5ddd9;--acento:#1f8a6b;--erro:#b3261e}}
@media (prefers-color-scheme: dark){{:root{{--fundo:#0f1513;--cartao:#161e1b;--texto:#e6eeea;--suave:#9aaba3;--borda:#2a3631;--acento:#4fc29c;--erro:#ff8a80}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--fundo);color:var(--texto);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;justify-content:center;padding:32px 16px}}
main{{width:100%;max-width:400px;background:var(--cartao);border:1px solid var(--borda);border-radius:14px;padding:28px}}
h1{{font-size:20px;margin:0 0 16px}}.marca{{font-weight:700;color:var(--acento);letter-spacing:.5px;margin-bottom:12px}}
.destino{{border:1px solid var(--acento);border-radius:10px;padding:12px 14px;margin-bottom:16px}}
.destino small{{color:var(--suave);display:block}}.destino b{{font-family:ui-monospace,Consolas,monospace;font-size:16px}}
p,li{{color:var(--suave)}}ul{{padding-left:20px;margin:6px 0 16px}}
label{{display:block;font-size:13px;margin:14px 0 6px;color:var(--suave)}}
input{{width:100%;padding:11px 12px;border:1px solid var(--borda);border-radius:8px;background:transparent;color:var(--texto);font-size:15px}}
input:focus{{outline:2px solid var(--acento);border-color:transparent}}
button{{width:100%;margin-top:22px;padding:12px;border:0;border-radius:8px;background:var(--acento);color:#fff;font-weight:600;font-size:15px;cursor:pointer}}
.erro{{color:var(--erro);border:1px solid var(--erro);border-radius:8px;padding:10px 12px;margin-bottom:8px}}
.rodape{{font-size:12.5px;text-align:center;margin-top:18px}}
</style></head><body><main>
<div class="marca">TACIO</div>
<h1>Autorizar acesso ao Tacio</h1>
<div class="destino"><small>O acesso será entregue a</small><b>{destino}</b>
<small>O aplicativo se apresenta como “{nome_app}”. Confira o endereço acima antes de digitar a senha.</small></div>
<p>Entre com o seu usuário do Sankhya. A conexão poderá <b>consultar</b>:</p>
<ul><li>vendas, compras, fornecedores e naturezas</li><li>estoque e ruptura por loja</li>
<li>entregas e SAM</li><li>contas a pagar e a receber, fluxo e saldo das contas</li></ul>
<p>Nada é incluído, alterado ou excluído no ERP. A sua senha não fica guardada no Tacio.</p>
{erro}
<form method="post" action="/login" autocomplete="on">
<input type="hidden" name="pedido" value="{pedido}">
{campo_cnpj}<label for="usuario">Usuário</label><input id="usuario" name="usuario" autocomplete="username" required value="{usuario}">
<label for="senha">Senha</label><input id="senha" name="senha" type="password" autocomplete="current-password" required>
<button type="submit">Autorizar acesso</button>
</form>
<p class="rodape">Para desconectar, remova o conector no seu assistente ou fale com o TI.</p>
</main></body></html>"""

_EXPIRADO = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Tacio</title></head>
<body style="font:15px system-ui,sans-serif;padding:32px 16px;max-width:420px;margin:auto">
<h1 style="font-size:20px">Pedido de acesso expirado</h1>
<p>Volte ao seu assistente (Claude ou ChatGPT) e conecte o Tacio de novo.</p></body></html>"""

_CABECALHOS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY",
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
               "Referrer-Policy": "no-referrer"}


def _pagina(provedor: ProvedorTacio, pedido: str, erro: str | None = None,
            cnpj: str = "", usuario: str = "", status: int = 200) -> Response:
    lido = provedor.ler_pedido(pedido)
    if lido is None:
        return HTMLResponse(_EXPIRADO, status_code=400, headers=_CABECALHOS)
    cliente, params = lido
    destino = urlparse(str(params.redirect_uri)).hostname or str(params.redirect_uri)
    nome_app = (cliente.client_name if cliente and cliente.client_name else "aplicativo sem nome")
    campo_cnpj = ('<label for="cnpj">CNPJ da empresa</label><input id="cnpj" name="cnpj" inputmode="numeric" '
                  f'required value="{html.escape(cnpj)}">\n') if provedor.cnpjs else ""
    corpo = _PAGINA.format(
        destino=html.escape(destino), nome_app=html.escape(nome_app), pedido=html.escape(pedido),
        campo_cnpj=campo_cnpj, usuario=html.escape(usuario),
        erro=f'<div class="erro">{html.escape(erro)}</div>' if erro else "")
    return HTMLResponse(corpo, status_code=status, headers=_CABECALHOS)


def registrar_rotas(srv, provedor: ProvedorTacio) -> None:
    """Rotas públicas da tela de login (o SDK não exige token nelas)."""

    @srv.custom_route("/login", methods=["GET"])
    async def tela_login(request: Request) -> Response:
        return _pagina(provedor, request.query_params.get("pedido", ""))

    @srv.custom_route("/login", methods=["POST"])
    async def enviar_login(request: Request) -> Response:
        form = await request.form()
        pedido, cnpj = str(form.get("pedido", "")), str(form.get("cnpj", ""))
        usuario, senha = str(form.get("usuario", "")), str(form.get("senha", ""))
        destino, erro = await provedor.entrar(pedido, cnpj, usuario, senha)
        if destino:
            return RedirectResponse(destino, status_code=302, headers={"Cache-Control": "no-store"})
        return _pagina(provedor, pedido, erro, cnpj=cnpj, usuario=usuario, status=400)

    @srv.custom_route("/saude", methods=["GET"])
    async def saude(request: Request) -> Response:
        return Response("ok", media_type="text/plain")
