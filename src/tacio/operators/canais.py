"""Canais de saída dos operators. Sem dependências externas (urllib + smtplib)."""
import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Any, Protocol


def _post_json(url: str, payload: dict[str, Any], timeout: int = 15) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status} ao enviar para {url.split('?')[0]}")


def _env(nome: str) -> str:
    valor = os.environ.get(nome)
    if not valor:
        raise RuntimeError(f"Variável de ambiente {nome} não definida.")
    return valor


class Canal(Protocol):
    nome: str

    def enviar(self, titulo: str, texto: str) -> None: ...


class Console:
    def __init__(self, nome: str, cfg: dict[str, Any]):
        self.nome = nome

    def enviar(self, titulo: str, texto: str) -> None:
        print(f"\n=== {titulo} ===\n{texto}\n", flush=True)


class Telegram:
    LIMITE = 4000

    def __init__(self, nome: str, cfg: dict[str, Any]):
        self.nome, self.token_env, self.chat_id = nome, cfg["token_env"], str(cfg["chat_id"])

    def enviar(self, titulo: str, texto: str) -> None:
        corpo = f"{titulo}\n\n{texto}"
        if len(corpo) > self.LIMITE:
            corpo = corpo[: self.LIMITE - 20] + "\n... (cortado)"
        url = f"https://api.telegram.org/bot{_env(self.token_env)}/sendMessage"
        _post_json(url, {"chat_id": self.chat_id, "text": corpo,
                         "disable_web_page_preview": True})


class Teams:
    """Webhook de Workflow do Teams ('Post to a channel when a webhook request is received')."""

    def __init__(self, nome: str, cfg: dict[str, Any]):
        self.nome, self.url_env = nome, cfg["url_env"]

    def enviar(self, titulo: str, texto: str) -> None:
        card = {
            "type": "AdaptiveCard", "version": "1.4",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": [
                {"type": "TextBlock", "text": titulo, "weight": "Bolder", "size": "Medium",
                 "wrap": True},
                {"type": "TextBlock", "text": texto, "wrap": True, "fontType": "Monospace"},
            ],
        }
        _post_json(_env(self.url_env), {
            "type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                             "content": card}],
        })


class Email:
    def __init__(self, nome: str, cfg: dict[str, Any]):
        self.nome, self.cfg = nome, cfg

    def enviar(self, titulo: str, texto: str) -> None:
        c = self.cfg
        msg = EmailMessage()
        msg["Subject"], msg["From"] = titulo, c["remetente"]
        msg["To"] = ", ".join(c["para"])
        msg.set_content(texto)
        with smtplib.SMTP(c["smtp_host"], c.get("smtp_porta", 587), timeout=20) as smtp:
            if c.get("tls", True):
                smtp.starttls()
            if c.get("usuario"):
                smtp.login(c["usuario"], _env(c["senha_env"]))
            smtp.send_message(msg)


_TIPOS = {"console": Console, "telegram": Telegram, "teams": Teams, "email": Email}


def montar_canais(cfg_canais: dict[str, dict[str, Any]]) -> dict[str, Canal]:
    canais: dict[str, Canal] = {}
    for nome, cfg in cfg_canais.items():
        tipo = cfg.get("tipo")
        if tipo not in _TIPOS:
            raise ValueError(f"Canal '{nome}': tipo '{tipo}' desconhecido ({', '.join(_TIPOS)}).")
        canais[nome] = _TIPOS[tipo](nome, cfg)
    return canais
