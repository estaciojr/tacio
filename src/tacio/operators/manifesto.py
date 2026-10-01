"""Manifesto de operator: um arquivo TOML por operator no diretório configurado.

Filtros são declarativos (sem eval): igualdade/lista em [operator.filtro] e
valor mínimo em [operator.minimo].
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cron import Cron


@dataclass
class Manifesto:
    id: str
    nome: str
    agenda: Cron
    tool: str
    canais: list[str]
    params: dict[str, Any] = field(default_factory=dict)
    descricao: str = ""
    chave: list[str] = field(default_factory=list)       # colunas que identificam um alerta
    campos_msg: list[str] = field(default_factory=list)  # colunas exibidas na mensagem
    filtro: dict[str, list[Any]] = field(default_factory=dict)
    minimo: dict[str, float] = field(default_factory=dict)
    max_itens_msg: int = 15
    avisar_resolvidos: bool = True
    enviar_se_vazio: bool = False
    analisar_com_llm: bool = False
    instrucao_llm: str = ""
    ativo: bool = True
    arquivo: str = ""


def carregar_manifesto(caminho: Path) -> Manifesto:
    with open(caminho, "rb") as f:
        raw = tomllib.load(f)["operator"]
    filtro = {k: (v if isinstance(v, list) else [v]) for k, v in raw.pop("filtro", {}).items()}
    minimo = {k: float(v) for k, v in raw.pop("minimo", {}).items()}
    agenda = Cron(raw.pop("agenda"))
    canais = raw.pop("canais")
    if isinstance(canais, str):
        canais = [canais]
    return Manifesto(agenda=agenda, canais=canais, filtro=filtro, minimo=minimo,
                     arquivo=str(caminho), nome=raw.pop("nome", raw["id"]), **raw)


def carregar_todos(diretorio: str | Path, tools: set[str], canais: set[str]) -> list[Manifesto]:
    """Carrega e valida todos os manifestos. Falha cedo, com o arquivo no erro."""
    manifestos: list[Manifesto] = []
    ids: set[str] = set()
    for arq in sorted(Path(diretorio).glob("*.toml")):
        try:
            m = carregar_manifesto(arq)
        except Exception as e:
            raise ValueError(f"{arq.name}: {e}") from e
        if m.id in ids:
            raise ValueError(f"{arq.name}: id '{m.id}' duplicado.")
        if m.tool not in tools:
            raise ValueError(f"{arq.name}: tool '{m.tool}' não existe nos packs ativos.")
        faltando = [c for c in m.canais if c not in canais]
        if faltando:
            raise ValueError(f"{arq.name}: canais não configurados no tenant: {faltando}")
        ids.add(m.id)
        manifestos.append(m)
    return manifestos
