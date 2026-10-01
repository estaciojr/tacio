"""Auditoria: uma linha JSON por chamada de tool (quem, o quê, quanto, quanto tempo)."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Auditoria:
    def __init__(self, arquivo: str | Path, tenant: str):
        self.arquivo = Path(arquivo)
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        self.tenant = tenant
        self._lock = threading.Lock()

    def registrar(self, tool: str, params: dict[str, Any], *, ms: float,
                  linhas: int | None = None, erro: str | None = None) -> None:
        evento = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tenant": self.tenant,
            "tool": tool,
            "params": params,
            "ms": round(ms, 1),
            "linhas": linhas,
            "erro": erro,
        }
        linha = json.dumps(evento, ensure_ascii=False, default=str)
        with self._lock, self.arquivo.open("a", encoding="utf-8") as f:
            f.write(linha + "\n")
