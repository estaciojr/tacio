"""Política central. Toda tool passa por aqui; nenhuma regra fica espalhada nos packs."""
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .config import PoliticaConfig

# CPF formatado ou 11 dígitos isolados (não pega CNPJ de 14 dígitos)
_CPF_RE = re.compile(r"(?<!\d)(\d{3})\.?(\d{3})\.?(\d{3})-?(\d{2})(?!\d)")
# Sinais típicos de UTF-8 lido como ISO-8859-1 ("Ã§", "Ã£", "Ã©"...)
_MOJIBAKE_RE = re.compile(r"[ÃÂ][\x80-\xBF¡-¿]")


class PoliticaViolada(Exception):
    """Erro de regra de negócio/política, devolvido ao agente como mensagem clara."""


def corrigir_encoding(texto: str) -> str:
    """Repara double-encoding UTF-8/ISO-8859-1. Se não for o caso, devolve intacto."""
    if not _MOJIBAKE_RE.search(texto):
        return texto
    try:
        return texto.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return texto


def mascarar_cpf(texto: str) -> str:
    return _CPF_RE.sub(lambda m: f"***.{m.group(2)}.***-**", texto)


def _valor_json(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("latin-1", errors="replace")
    return v


@dataclass
class Politica:
    cfg: PoliticaConfig
    codemp_permitidas: list[int]

    # --- escopo -----------------------------------------------------------
    def validar_codemp(self, codemp: int | None) -> list[int] | None:
        """Devolve a lista de CODEMP efetiva para o filtro, ou None = sem filtro."""
        if codemp is not None:
            if self.codemp_permitidas and codemp not in self.codemp_permitidas:
                raise PoliticaViolada(f"Empresa {codemp} fora do escopo deste servidor.")
            return [codemp]
        return self.codemp_permitidas or None

    # --- saída ------------------------------------------------------------
    def limpar_linha(self, linha: dict[str, Any]) -> dict[str, Any]:
        mascaradas = {c.upper() for c in self.cfg.colunas_mascaradas}
        out: dict[str, Any] = {}
        for col, v in linha.items():
            v = _valor_json(v)
            if isinstance(v, str):
                v = corrigir_encoding(v.strip())
                if col.upper() in mascaradas:
                    v = mascarar_cpf(v) if _CPF_RE.search(v) else v
                elif self.cfg.mascarar_cpf_em_texto:
                    v = mascarar_cpf(v)
            out[col] = v
        return out

    def resultado(self, linhas: list[dict[str, Any]], truncado: bool, **extra: Any) -> dict[str, Any]:
        """Formato padrão de retorno de toda tool de leitura."""
        res = {
            "linhas": [self.limpar_linha(l) for l in linhas],
            "total_retornado": len(linhas),
            "truncado": truncado,
        }
        if truncado:
            res["aviso"] = (
                f"Resultado limitado a {len(linhas)} linhas. "
                "Refine os filtros ou peça um agregado."
            )
        res.update(extra)
        return res


def parse_data(valor: str, campo: str = "data") -> date:
    """Aceita AAAA-MM-DD (padrão do agente) ou AAAAMMDD."""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(valor.strip(), fmt).date()
        except ValueError:
            continue
    raise PoliticaViolada(f"{campo} inválida: '{valor}'. Use AAAA-MM-DD.")


def validar_periodo(data_inicio: str, data_fim: str, max_dias: int = 366) -> tuple[date, date]:
    ini, fim = parse_data(data_inicio, "data_inicio"), parse_data(data_fim, "data_fim")
    if ini > fim:
        raise PoliticaViolada("data_inicio é posterior a data_fim.")
    if (fim - ini).days > max_dias:
        raise PoliticaViolada(f"Período máximo de {max_dias} dias.")
    return ini, fim
