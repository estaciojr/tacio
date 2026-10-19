"""Classificação das TOPs por natureza (revenda, transferência, bonificação...).

Fica num TOML por tenant (ex.: tenants/dalla.tops.toml), fora do código: cada cliente tem
o próprio, e uma futura tela de configuração edita o mesmo formato.
"""
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .policy import PoliticaViolada


@dataclass
class Natureza:
    id: str
    nome: str
    sinal: int = 1       # 1 soma, -1 subtrai (devolução)
    compra: bool = False  # entra no padrão das tools de compra


@dataclass
class Checagens:
    avisar_top_nao_classificada: bool = True
    revenda_sem_parceiro_do_grupo: bool = True
    transferencia_so_entre_empresas: bool = True
    # Empresas do grupo que vendem de verdade para as outras (ex.: importadora do grupo):
    # notas de revenda desses parceiros não geram aviso.
    parceiros_grupo_fornecedores: list[int] = field(default_factory=list)
    # Natureza -> campo S/N da TGFTOP que deve concordar com ela (ex.: bonificacao =
    # "BONIFICACAO"). Divergência nos dois sentidos vira aviso.
    campos_top: dict[str, str] = field(default_factory=dict)


@dataclass
class Classificacao:
    naturezas: dict[str, Natureza]
    tops: dict[int, str]
    # TOPs de pedido (TIPMOV 'O') ficam à parte: misturadas em `tops`, as tools de nota
    # passariam a contar pedido como compra.
    tops_pedido: dict[int, str] = field(default_factory=dict)
    checagens: Checagens = field(default_factory=Checagens)
    pendencias: list[str] = field(default_factory=list)

    def natureza_da_top(self, codtipoper: int) -> str | None:
        return self.tops.get(codtipoper)

    def validar(self, naturezas: list[str] | None) -> list[str]:
        """Naturezas pedidas pelo agente; None/vazio = as marcadas como compra."""
        if not naturezas:
            padrao = [n.id for n in self.naturezas.values() if n.compra]
            if not padrao:
                raise PoliticaViolada("Nenhuma natureza marcada como compra na classificação.")
            return padrao
        desconhecidas = [n for n in naturezas if n not in self.naturezas]
        if desconhecidas:
            raise PoliticaViolada(
                f"Natureza desconhecida: {', '.join(desconhecidas)}. "
                f"Válidas: {', '.join(self.naturezas)}.")
        return list(dict.fromkeys(naturezas))

    def tops_de(self, naturezas: list[str]) -> list[int]:
        return sorted(t for t, n in self.tops.items() if n in naturezas)

    def natureza_do_pedido(self, codtipoper: int) -> str | None:
        return self.tops_pedido.get(codtipoper)

    def tops_pedido_de(self, naturezas: list[str]) -> list[int]:
        return sorted(t for t, n in self.tops_pedido.items() if n in naturezas)

    def tops_negativas(self, tops: list[int]) -> list[int]:
        return [t for t in tops if self.naturezas[self.tops[t]].sinal < 0]


def _checagens(raw: dict) -> Checagens:
    chk = Checagens(**raw)
    # Parceiros são interpolados no SQL: só inteiros passam daqui.
    chk.parceiros_grupo_fornecedores = [int(c) for c in chk.parceiros_grupo_fornecedores]
    # Campos da TOP também são interpolados: só identificadores simples.
    for nat, campo in chk.campos_top.items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", str(campo).upper()):
            raise ValueError(f"checagens.campos_top.{nat}: campo '{campo}' inválido.")
    chk.campos_top = {nat: str(c).upper() for nat, c in chk.campos_top.items()}
    return chk


def _tops(raw: dict, naturezas: dict[str, Natureza], secao: str) -> dict[int, str]:
    tops: dict[int, str] = {}
    for chave, nat in raw.items():
        codigo = int(chave)  # TOPs são interpoladas no SQL: só inteiros passam daqui
        if nat not in naturezas:
            raise ValueError(f"[{secao}] TOP {codigo}: natureza '{nat}' não definida em [naturezas].")
        tops[codigo] = nat
    return tops


def carregar_classificacao(caminho: str | Path) -> Classificacao:
    with open(caminho, "rb") as f:
        raw = tomllib.load(f)
    naturezas = {
        nid: Natureza(id=nid, nome=d.get("nome", nid), sinal=int(d.get("sinal", 1)),
                      compra=bool(d.get("compra", False)))
        for nid, d in raw.get("naturezas", {}).items()
    }
    for n in naturezas.values():
        if n.sinal not in (1, -1):
            raise ValueError(f"Natureza '{n.id}': sinal deve ser 1 ou -1.")
    return Classificacao(
        naturezas=naturezas,
        tops=_tops(raw.get("tops", {}), naturezas, "tops"),
        tops_pedido=_tops(raw.get("tops_pedido", {}), naturezas, "tops_pedido"),
        checagens=_checagens(raw.get("checagens", {})),
        pendencias=list(raw.get("pendencias", {}).get("itens", [])),
    )
