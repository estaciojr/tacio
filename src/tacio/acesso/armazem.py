"""Guarda do OAuth em SQLite: clientes registrados, pedidos de login, códigos e tokens.

Tokens e códigos são gravados pelo hash SHA-256: quem ler o arquivo não consegue usá-los.
Registro vencido some na leitura.
"""
import hashlib
import sqlite3
import threading
import time
from pathlib import Path


def hash_segredo(valor: str) -> str:
    return hashlib.sha256(valor.encode()).hexdigest()


class Armazem:
    def __init__(self, arquivo: str | Path):
        if str(arquivo) != ":memory:":
            Path(arquivo).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(arquivo), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._con.execute("""CREATE TABLE IF NOT EXISTS registros (
                tipo TEXT NOT NULL, chave TEXT NOT NULL, valor TEXT NOT NULL, expira REAL,
                PRIMARY KEY (tipo, chave))""")
            self._con.commit()

    def gravar(self, tipo: str, chave: str, valor: str, expira: float | None) -> None:
        with self._lock:
            self._con.execute("INSERT OR REPLACE INTO registros VALUES (?, ?, ?, ?)",
                              (tipo, chave, valor, expira))
            self._con.commit()

    def ler(self, tipo: str, chave: str) -> str | None:
        with self._lock:
            linha = self._con.execute("SELECT valor, expira FROM registros WHERE tipo = ? AND chave = ?",
                                      (tipo, chave)).fetchone()
            if linha is None:
                return None
            valor, expira = linha
            if expira is not None and expira < time.time():
                self._con.execute("DELETE FROM registros WHERE tipo = ? AND chave = ?", (tipo, chave))
                self._con.commit()
                return None
            return valor

    def apagar(self, tipo: str, chave: str) -> None:
        with self._lock:
            self._con.execute("DELETE FROM registros WHERE tipo = ? AND chave = ?", (tipo, chave))
            self._con.commit()

    def limpar_vencidos(self) -> int:
        with self._lock:
            n = self._con.execute("DELETE FROM registros WHERE expira IS NOT NULL AND expira < ?",
                                  (time.time(),)).rowcount
            self._con.commit()
            return n
