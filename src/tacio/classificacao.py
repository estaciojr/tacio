"""Classificação das TOPs por natureza (revenda, transferência, bonificação...).

Fica num TOML por tenant (ex.: tenants/dalla.tops.toml), fora do código: cada cliente tem
o próprio, e uma futura tela de configuração edita o mesmo formato.
"""
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


@dataclass
class Classificacao:
    naturezas: dict[str, Natureza]
    tops: dict[int, str]
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

    def tops_negativas(self, tops: list[int]) -> list[int]:
        return [t for t in tops if self.naturezas[self.tops[t]].sinal < 0]


def _checagens(raw: dict) -> Checagens:
    chk = Checagens(**raw)
    # Parceiros são interpolados no SQL: só inteiros passam daqui.
    chk.parceiros_grupo_fornecedores = [int(c) for c in chk.parceiros_grupo_fornecedores]
    return chk


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
    tops: dict[int, str] = {}
    for chave, nat in raw.get("tops", {}).items():
        codigo = int(chave)  # TOPs são interpoladas no SQL: só inteiros passam daqui
        if nat not in naturezas:
            raise ValueError(f"TOP {codigo}: natureza '{nat}' não definida em [naturezas].")
        tops[codigo] = nat
    return Classificacao(
        naturezas=naturezas,
        tops=tops,
        checagens=_checagens(raw.get("checagens", {})),
        pendencias=list(raw.get("pendencias", {}).get("itens", [])),
    )
