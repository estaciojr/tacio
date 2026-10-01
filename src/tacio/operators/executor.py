"""Executor e agendador dos operators do Tacio."""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..adapters.sql_read import LeitorSql
from ..audit import Auditoria
from ..config import TenantConfig
from ..pack import carregar_tools, montar_contexto
from . import llm
from .canais import Canal, montar_canais
from .estado import Diferenca, EstadoOperators
from .manifesto import Manifesto, carregar_todos

log = logging.getLogger("tacio.operators")


@dataclass
class Relatorio:
    operator: str
    titulo: str = ""
    texto: str = ""
    linhas: int = 0
    novos: int = 0
    persistentes: int = 0
    resolvidos: int = 0
    enviado: bool = False
    erro: str | None = None


# --- formatação ----------------------------------------------------------------
def _fmt(v: Any) -> str:
    if isinstance(v, float):
        txt = f"{v:,.2f}" if abs(v) >= 1000 or v != int(v) else f"{int(v)}"
        return txt.replace(",", "X").replace(".", ",").replace("X", ".")
    return "" if v is None else str(v)


def _tabela(linhas: list[dict[str, Any]], campos: list[str], maximo: int) -> str:
    if not linhas:
        return ""
    cols = campos or list(linhas[0])
    saida = [" | ".join(cols)]
    saida += ["- " + " | ".join(_fmt(l.get(c)) for c in cols) for l in linhas[:maximo]]
    if len(linhas) > maximo:
        saida.append(f"... e mais {len(linhas) - maximo}")
    return "\n".join(saida)


def _aplicar_filtros(m: Manifesto, linhas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def passa(l: dict[str, Any]) -> bool:
        for col, aceitos in m.filtro.items():
            if l.get(col) not in aceitos:
                return False
        for col, minimo in m.minimo.items():
            v = l.get(col)
            if not isinstance(v, (int, float)) or v < minimo:
                return False
        return True
    return [l for l in linhas if passa(l)]


def montar_mensagem(m: Manifesto, d: Diferenca, truncado: bool) -> tuple[str, str]:
    titulo = (f"[Tacio] {m.nome}: {len(d.novos)} novos, {len(d.resolvidos)} resolvidos, "
              f"{len(d.persistentes)} seguem abertos")
    partes: list[str] = []
    if d.novos:
        partes.append("NOVOS\n" + _tabela(d.novos, m.campos_msg, m.max_itens_msg))
    if d.resolvidos and m.avisar_resolvidos:
        partes.append("RESOLVIDOS\n" + _tabela(d.resolvidos, m.campos_msg, min(10, m.max_itens_msg)))
    if not partes:
        partes.append("Nada novo. Tudo dentro do esperado.")
    if truncado:
        partes.append("Atenção: o resultado veio limitado; pode haver mais itens.")
    return titulo, "\n\n".join(partes)


# --- execução --------------------------------------------------------------------
class Executor:
    def __init__(self, tenant: TenantConfig, sql: LeitorSql | None = None,
                 canais: dict[str, Canal] | None = None):
        self.tenant = tenant
        self.ctx = montar_contexto(tenant, sql)
        self.tools = carregar_tools(tenant)
        self.canais = canais if canais is not None else montar_canais(tenant.canais)
        self.estado = EstadoOperators(tenant.operators.estado)
        self.auditoria = Auditoria(tenant.auditoria_arquivo, tenant.id)

    def manifestos(self) -> list[Manifesto]:
        return carregar_todos(self.tenant.operators.diretorio, set(self.tools), set(self.canais))

    def executar(self, m: Manifesto, simular: bool = False) -> Relatorio:
        """Roda um operator. simular=True monta a mensagem sem enviar e sem gravar estado."""
        inicio, t0 = datetime.now(), time.perf_counter()
        rel = Relatorio(operator=m.id)
        try:
            res = self.tools[m.tool].fn(self.ctx, **m.params)
            linhas = _aplicar_filtros(m, res.get("linhas", []))
            d = self.estado.comparar(m.id, linhas, m.chave)
            rel.linhas, rel.novos = len(linhas), len(d.novos)
            rel.persistentes, rel.resolvidos = len(d.persistentes), len(d.resolvidos)
            rel.titulo, rel.texto = montar_mensagem(m, d, bool(res.get("truncado")))

            if m.analisar_com_llm and d.novos:
                try:
                    analise = llm.analisar(
                        self.tenant.operators.llm_modelo, self.tenant.operators.llm_api_key_env,
                        m.nome, m.instrucao_llm, d.novos, len(d.persistentes), len(d.resolvidos))
                    if analise:
                        rel.texto = f"ANÁLISE\n{analise}\n\n{rel.texto}"
                except Exception as e:
                    log.warning("operator %s: análise LLM falhou: %s", m.id, e)
                    rel.texto += "\n\n(análise automática indisponível nesta execução)"

            deve_enviar = bool(d.novos) or (m.avisar_resolvidos and bool(d.resolvidos)) \
                or m.enviar_se_vazio
            if simular:
                return rel
            if deve_enviar:
                falhas = self._enviar(m, rel.titulo, rel.texto)
                rel.enviado = len(falhas) < len(m.canais)
                if not rel.enviado:
                    raise RuntimeError("Nenhum canal aceitou o envio: " + "; ".join(falhas))
                if falhas:
                    rel.erro = "; ".join(falhas)
            # Só grava o estado se o alerta saiu (ou não havia o que enviar): falha = reenvia depois.
            self.estado.gravar(m.id, linhas, m.chave)
        except Exception as e:
            rel.erro = f"{type(e).__name__}: {e}"
            log.error("operator %s falhou: %s", m.id, rel.erro)
            if not simular:
                self._enviar(m, f"[Tacio] Falha no operator {m.nome}",
                             "O monitor não conseguiu rodar. Detalhe na auditoria do Tacio.")
        finally:
            ms = (time.perf_counter() - t0) * 1000
            if not simular:
                self.estado.registrar_execucao(m.id, inicio, ms, rel.linhas, rel.novos,
                                               rel.resolvidos, rel.enviado, rel.erro)
                self.auditoria.registrar(f"operator:{m.id}", m.params, ms=ms,
                                         linhas=rel.linhas, erro=rel.erro)
        return rel

    def _enviar(self, m: Manifesto, titulo: str, texto: str) -> list[str]:
        falhas = []
        for nome in m.canais:
            try:
                self.canais[nome].enviar(titulo, texto)
            except Exception as e:
                falhas.append(f"{nome}: {e}")
                log.error("operator %s: canal %s falhou: %s", m.id, nome, e)
        return falhas


# --- agendador -------------------------------------------------------------------
class Agendador:
    """Loop simples: a cada ciclo, avalia os minutos desde a última checagem (sem pular minutos
    se uma execução demorar) e recarrega os manifestos quando algum arquivo muda."""

    def __init__(self, executor: Executor, intervalo_s: int = 20, recuperar_min: int = 10):
        self.ex, self.intervalo_s, self.recuperar_min = executor, intervalo_s, recuperar_min
        self._assinatura: tuple = ()
        self._manifestos: list[Manifesto] = []
        self._ultimo = datetime.now().replace(second=0, microsecond=0)

    def _recarregar_se_mudou(self) -> None:
        pasta = Path(self.ex.tenant.operators.diretorio)
        assinatura = tuple(sorted((p.name, p.stat().st_mtime) for p in pasta.glob("*.toml")))
        if assinatura != self._assinatura:
            self._manifestos = self.ex.manifestos()
            self._assinatura = assinatura
            log.info("operators carregados: %s", ", ".join(m.id for m in self._manifestos) or "-")

    def ciclo(self, agora: datetime | None = None) -> list[Relatorio]:
        agora = (agora or datetime.now()).replace(second=0, microsecond=0)
        try:
            self._recarregar_se_mudou()
        except Exception as e:  # manifesto inválido: mantém os anteriores e avisa no log
            log.error("erro ao recarregar operators: %s", e)
        inicio = max(self._ultimo + timedelta(minutes=1),
                     agora - timedelta(minutes=self.recuperar_min))
        relatorios: list[Relatorio] = []
        minuto = inicio
        while minuto <= agora:
            for m in self._manifestos:
                if m.ativo and m.agenda.bate(minuto):
                    log.info("executando %s (%s)", m.id, minuto.strftime("%H:%M"))
                    relatorios.append(self.ex.executar(m))
            minuto += timedelta(minutes=1)
        self._ultimo = max(self._ultimo, agora)
        return relatorios

    def rodar(self) -> None:
        log.info("Tacio Operators iniciado (tenant %s).", self.ex.tenant.id)
        while True:
            self.ciclo()
            time.sleep(self.intervalo_s)
