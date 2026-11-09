"""Confere usuário e senha no Sankhya (MobileLoginSP.login) e encerra a sessão em seguida.

O Tacio não guarda a senha nem a sessão do Sankhya: o login só prova quem é o usuário.
O MobileLoginSP responde sem o token do Gateway (conferido em 19/10/2026); os demais
serviços do MGE é que exigem o JWT no header 'token'.
"""
import base64
import json
import urllib.request
from dataclasses import dataclass


class ErroSankhya(Exception):
    """O Sankhya não respondeu ou respondeu algo inesperado (não é senha errada)."""


@dataclass
class UsuarioSankhya:
    nome: str     # NOMUSU como digitado, em maiúsculas
    codusu: int | None


class LoginSankhya:
    def __init__(self, url: str, timeout_s: int = 15):
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s

    def _chamar(self, servico: str, corpo: dict, sessao: str | None = None) -> dict:
        url = f"{self.url}/mge/service.sbr?serviceName={servico}&outputType=json"
        headers = {"Content-Type": "application/json"}
        if sessao:
            url += f"&mgeSession={sessao}"
            headers["Cookie"] = f"JSESSIONID={sessao}"
        dados = json.dumps({"serviceName": servico, "requestBody": corpo}).encode()
        req = urllib.request.Request(url, data=dados, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read().decode("latin1"))  # o MGE responde em ISO-8859-1
        except (OSError, ValueError) as e:
            raise ErroSankhya(f"Sankhya indisponível: {e}") from e

    def validar(self, usuario: str, senha: str) -> UsuarioSankhya | None:
        """Devolve o usuário se a senha confere, None se o Sankhya recusar."""
        nome = usuario.strip().upper()
        if not nome or not senha:
            return None
        r = self._chamar("MobileLoginSP.login", {
            "NOMUSU": {"$": nome}, "INTERNO": {"$": senha}, "KEEPCONNECTED": {"$": "N"}})
        if str(r.get("status")) != "1":
            if "inv" in (r.get("statusMessage") or "").lower():  # "Usuário/Senha inválido."
                return None
            raise ErroSankhya(f"login recusado: {r.get('statusMessage')}")
        corpo = r.get("responseBody") or {}
        codusu = None
        try:
            codusu = int(base64.b64decode(corpo["idusu"]["$"]).decode())
        except (KeyError, ValueError, TypeError):
            pass
        sessao = (corpo.get("jsessionid") or {}).get("$")
        if sessao:
            try:
                self._chamar("MobileLoginSP.logout", {}, sessao)
            except ErroSankhya:
                pass  # a sessão expira sozinha; não impede o login
        return UsuarioSankhya(nome=nome, codusu=codusu)
