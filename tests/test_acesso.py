"""Login OAuth de ponta a ponta: servidor HTTP de verdade numa porta local, Sankhya falso."""
import base64
import hashlib
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
import uvicorn

from tacio.acesso.armazem import Armazem
from tacio.acesso.oauth import ProvedorTacio
from tacio.acesso.sankhya_login import ErroSankhya, UsuarioSankhya
from tacio.config import OAuthConfig
from tacio.server import app_oauth

from test_nucleo import LeitorFalso, _tenant

CNPJ = "12.345.678/0001-90"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
SENHAS = {"ANA": "certa", "BRUNO": "certa", "CLAUDE.TI": "certa"}


class SankhyaFalso:
    def __init__(self):
        self.fora_do_ar = False
        self.chamadas = 0

    def validar(self, usuario, senha):
        self.chamadas += 1
        if self.fora_do_ar:
            raise ErroSankhya("timeout")
        nome = usuario.strip().upper()
        return UsuarioSankhya(nome, 7) if SENHAS.get(nome) == senha else None


class _SemRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_abrir = urllib.request.build_opener(_SemRedirect).open


def _req(url, dados=None, headers=None, metodo=None):
    """(status, headers, corpo) sem seguir redirects nem lançar em 4xx."""
    if isinstance(dados, dict):
        dados = urllib.parse.urlencode(dados).encode()
    req = urllib.request.Request(url, data=dados, headers=headers or {}, method=metodo)
    try:
        with _abrir(req, timeout=10) as r:
            return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


@pytest.fixture
def servidor(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    url = f"http://127.0.0.1:{porta}"
    tenant = _tenant(tmp_path)
    tenant.oauth = OAuthConfig(url_publica=url, sankhya_url="http://sankhya.invalido", cnpjs=[CNPJ],
                               usuarios_permitidos=["ANA", "CLAUDE.TI"], usuarios_sql_livre=["CLAUDE.TI"])
    tenant.politica.sql_livre = True
    tenant.politica.tabelas_sql_livre = ["TGFPRO"]
    sankhya = SankhyaFalso()
    provedor = ProvedorTacio(tenant.oauth, tenant.id, sankhya, Armazem(tmp_path / "oauth.db"))
    srv = uvicorn.Server(uvicorn.Config(app_oauth(tenant, provedor, sql=LeitorFalso()),
                                        host="127.0.0.1", port=porta, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield {"url": url, "sankhya": sankhya, "tmp": tmp_path, "provedor": provedor}
    srv.should_exit = True
    t.join(timeout=5)


def _registrar(url):
    st, _, corpo = _req(f"{url}/register", json.dumps({
        "redirect_uris": [REDIRECT], "client_name": "Claude", "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}).encode(),
        {"Content-Type": "application/json"})
    assert st == 201, corpo
    return json.loads(corpo)["client_id"]


def _iniciar(url, client_id):
    """/authorize -> devolve (pedido da tela de login, verifier, state)."""
    verifier = secrets.token_urlsafe(48)
    desafio = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(8)
    st, h, _ = _req(f"{url}/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": state,
        "code_challenge": desafio, "code_challenge_method": "S256", "resource": f"{url}/mcp"}))
    assert st == 302
    destino = urllib.parse.urlparse(h["Location"])
    assert destino.path == "/login"
    return urllib.parse.parse_qs(destino.query)["pedido"][0], verifier, state


def _login(url, pedido, usuario="ANA", senha="certa", cnpj=CNPJ):
    return _req(f"{url}/login", {"pedido": pedido, "cnpj": cnpj, "usuario": usuario, "senha": senha})


def _token_completo(url, usuario="ANA"):
    client_id = _registrar(url)
    pedido, verifier, state = _iniciar(url, client_id)
    st, h, corpo = _login(url, pedido, usuario=usuario)
    assert st == 302, corpo
    volta = urllib.parse.urlparse(h["Location"])
    q = urllib.parse.parse_qs(volta.query)
    assert f"{volta.scheme}://{volta.netloc}{volta.path}" == REDIRECT and q["state"] == [state]
    st, _, corpo = _req(f"{url}/token", {"grant_type": "authorization_code", "code": q["code"][0],
                                          "redirect_uri": REDIRECT, "client_id": client_id,
                                          "code_verifier": verifier})
    assert st == 200, corpo
    return client_id, json.loads(corpo), q["code"][0], verifier


def _mcp(url, token, metodo, params, sessao=None, ident=1):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sessao:
        headers["mcp-session-id"] = sessao
    corpo = {"jsonrpc": "2.0", "method": metodo, "params": params}
    if ident is not None:
        corpo["id"] = ident
    st, h, texto = _req(f"{url}/mcp", json.dumps(corpo).encode(), headers)
    dados = [json.loads(l[5:]) for l in texto.splitlines() if l.startswith("data:")]
    return st, h, (dados[-1] if dados else (json.loads(texto) if texto.strip().startswith("{") else texto))


def _sessao(url, token):
    st, h, r = _mcp(url, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "teste", "version": "0"}})
    assert st == 200, r
    sessao = h.get("mcp-session-id")
    _mcp(url, token, "notifications/initialized", {}, sessao, ident=None)
    return sessao


def test_metadados_anunciam_o_login(servidor):
    url = servidor["url"]
    st, _, corpo = _req(f"{url}/.well-known/oauth-authorization-server")
    meta = json.loads(corpo)
    assert st == 200 and meta["issuer"].rstrip("/") == url and meta["registration_endpoint"].endswith("/register")
    st, h, _ = _mcp(url, None, "initialize", {})
    assert st == 401 and "resource_metadata" in h.get("WWW-Authenticate", "")


def test_fluxo_completo_e_auditoria_com_usuario(servidor):
    url = servidor["url"]
    _, tok, _, _ = _token_completo(url)
    assert tok["token_type"].lower() == "bearer" and tok["refresh_token"]
    sessao = _sessao(url, tok["access_token"])
    st, _, r = _mcp(url, tok["access_token"], "tools/call",
                    {"name": "compras_por_fornecedor",
                     "arguments": {"data_inicio": "2026-09-01", "data_fim": "2026-09-30"}}, sessao, ident=2)
    assert st == 200 and not r["result"].get("isError"), r
    evento = json.loads((servidor["tmp"] / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert evento["usuario"] == "teste:ANA" and evento["tool"] == "compras_por_fornecedor"
    # a senha nunca vai para o banco do OAuth
    assert b"certa" not in (servidor["tmp"] / "oauth.db").read_bytes()


def test_token_invalido_e_recusado(servidor):
    st, _, _ = _mcp(servidor["url"], "token-inventado", "initialize", {})
    assert st == 401


def test_codigo_vale_uma_vez_e_exige_o_verifier(servidor):
    url = servidor["url"]
    client_id = _registrar(url)
    pedido, verifier, _ = _iniciar(url, client_id)
    _, h, _ = _login(url, pedido)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(h["Location"]).query)["code"][0]
    base = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client_id}
    st, _, _ = _req(f"{url}/token", {**base, "code_verifier": "errado" * 8})
    assert st == 400  # PKCE
    st, _, _ = _req(f"{url}/token", {**base, "code_verifier": verifier})
    assert st == 200
    st, _, _ = _req(f"{url}/token", {**base, "code_verifier": verifier})
    assert st == 400  # já usado


@pytest.mark.parametrize("dados,mensagem", [
    ({"senha": "errada"}, "Usuário ou senha inválidos"),
    ({"cnpj": "99.999.999/0001-99"}, "CNPJ não habilitado"),
    ({"usuario": "BRUNO"}, "ainda não foi liberado"),
])
def test_login_recusado_fica_na_tela(servidor, dados, mensagem):
    url = servidor["url"]
    pedido, _, _ = _iniciar(url, _registrar(url))
    st, _, corpo = _login(url, pedido, **dados)
    assert st == 400 and mensagem in corpo
    assert "certa" not in corpo and "errada" not in corpo  # senha não volta para a página


def test_bloqueia_apos_cinco_falhas(servidor):
    url = servidor["url"]
    pedido, _, _ = _iniciar(url, _registrar(url))
    for _ in range(5):
        _login(url, pedido, senha="errada")
    antes = servidor["sankhya"].chamadas
    st, _, corpo = _login(url, pedido)  # até a senha certa espera
    assert st == 400 and "Aguarde 15 minutos" in corpo
    assert servidor["sankhya"].chamadas == antes  # nem chega ao Sankhya


def test_sankhya_fora_do_ar(servidor):
    url = servidor["url"]
    pedido, _, _ = _iniciar(url, _registrar(url))
    servidor["sankhya"].fora_do_ar = True
    st, _, corpo = _login(url, pedido)
    assert st == 400 and "Não foi possível falar com o Sankhya" in corpo


def test_pedido_inexistente(servidor):
    st, _, corpo = _req(f"{servidor['url']}/login?pedido=inventado")
    assert st == 400 and "expirado" in corpo


def test_sql_livre_so_para_quem_esta_liberado(servidor):
    url = servidor["url"]
    for usuario, bloqueado in (("ANA", True), ("CLAUDE.TI", False)):
        _, tok, _, _ = _token_completo(url, usuario)
        sessao = _sessao(url, tok["access_token"])
        _, _, r = _mcp(url, tok["access_token"], "tools/call",
                       {"name": "sql_consulta", "arguments": {"sql": "SELECT CODPROD FROM TGFPRO"}},
                       sessao, ident=3)
        texto = json.dumps(r, ensure_ascii=False)
        assert ("não está liberada para o seu usuário" in texto) == bloqueado, texto


def test_refresh_gira_e_o_antigo_morre(servidor):
    url = servidor["url"]
    client_id, tok, _, _ = _token_completo(url)
    pedir = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id}
    st, _, corpo = _req(f"{url}/token", pedir)
    assert st == 200, corpo
    novo = json.loads(corpo)
    assert novo["access_token"] != tok["access_token"]
    st, _, _ = _req(f"{url}/token", pedir)
    assert st == 400  # refresh antigo não serve mais
    assert _mcp(url, tok["access_token"], "initialize", {})[0] == 401  # nem o acesso antigo
    assert _mcp(url, novo["access_token"], "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "t", "version": "0"}})[0] == 200


def test_tirar_da_lista_corta_o_acesso(servidor):
    url = servidor["url"]
    _, tok, _, _ = _token_completo(url)
    servidor["provedor"].permitidos.discard("ANA")
    assert _mcp(url, tok["access_token"], "initialize", {})[0] == 401


def test_tela_escapa_html_do_nome_do_app(servidor):
    url = servidor["url"]
    st, _, corpo = _req(f"{url}/register", json.dumps({
        "redirect_uris": [REDIRECT], "client_name": "<script>x</script>",
        "token_endpoint_auth_method": "none"}).encode(), {"Content-Type": "application/json"})
    pedido, _, _ = _iniciar(url, json.loads(corpo)["client_id"])
    st, h, pagina = _req(f"{url}/login?pedido={pedido}")
    assert st == 200 and "<script>x" not in pagina and "&lt;script&gt;" in pagina
    assert "claude.ai" in pagina and h["X-Frame-Options"] == "DENY"
