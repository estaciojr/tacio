"""Estado dos operators em SQLite: evita repetir alerta e detecta resolvidos."""
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class Diferenca:
    novos: list[dict[str, Any]] = field(default_factory=list)
    persistentes: list[dict[str, Any]] = field(default_factory=list)
    resolvidos: list[dict[str, Any]] = field(default_factory=list)


class EstadoOperators:
    def __init__(self, arquivo: str | Path):
        Path(arquivo).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(arquivo, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS alertas (
                operator TEXT NOT NULL, chave TEXT NOT NULL, dados TEXT NOT NULL,
                desde TEXT NOT NULL, visto_em TEXT NOT NULL,
                PRIMARY KEY (operator, chave));
            CREATE TABLE IF NOT EXISTS execucoes (
                operator TEXT NOT NULL, inicio TEXT NOT NULL, ms REAL,
                linhas INTEGER, novos INTEGER, resolvidos INTEGER,
                enviado INTEGER, erro TEXT);
        """)

    @staticmethod
    def _chave(linha: dict[str, Any], colunas: list[str]) -> str:
        cols = colunas or sorted(linha)
        return "|".join(str(linha.get(c)) for c in cols)

    def comparar(self, operator: str, linhas: list[dict[str, Any]], colunas_chave: list[str]
                 ) -> Diferenca:
        """Calcula a diferença contra o estado salvo, sem gravar nada."""
        atuais = {self._chave(l, colunas_chave): l for l in linhas}
        salvos = {
            k: json.loads(d)
            for k, d in self.db.execute(
                "SELECT chave, dados FROM alertas WHERE operator = ?", (operator,))
        }
        return Diferenca(
            novos=[l for k, l in atuais.items() if k not in salvos],
            persistentes=[l for k, l in atuais.items() if k in salvos],
            resolvidos=[d for k, d in salvos.items() if k not in atuais],
        )

    def gravar(self, operator: str, linhas: list[dict[str, Any]], colunas_chave: list[str]) -> None:
        """Substitui o estado do operator pelo resultado atual (preserva 'desde' dos persistentes)."""
        agora = datetime.now().isoformat(timespec="seconds")
        atuais = {self._chave(l, colunas_chave): l for l in linhas}
        with self.db:
            desde = dict(self.db.execute(
                "SELECT chave, desde FROM alertas WHERE operator = ?", (operator,)))
            self.db.execute("DELETE FROM alertas WHERE operator = ?", (operator,))
            self.db.executemany(
                "INSERT INTO alertas VALUES (?, ?, ?, ?, ?)",
                [(operator, k, json.dumps(l, ensure_ascii=False, default=str),
                  desde.get(k, agora), agora) for k, l in atuais.items()],
            )

    def registrar_execucao(self, operator: str, inicio: datetime, ms: float, linhas: int,
                           novos: int, resolvidos: int, enviado: bool, erro: str | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO execucoes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (operator, inicio.isoformat(timespec="seconds"), round(ms, 1),
                 linhas, novos, resolvidos, int(enviado), erro))

    def ultima_execucao(self, operator: str) -> dict[str, Any] | None:
        cur = self.db.execute(
            "SELECT inicio, ms, linhas, novos, resolvidos, enviado, erro FROM execucoes "
            "WHERE operator = ? ORDER BY inicio DESC LIMIT 1", (operator,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([c[0] for c in cur.description], row))
