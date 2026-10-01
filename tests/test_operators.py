import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tacio.adapters.sql_read import validar_somente_leitura
from tacio.config import HttpConfig, OperatorsConfig, PoliticaConfig, SqlConfig, TenantConfig
from tacio.operators import canais as canais_mod
from tacio.operators import llm as llm_mod
from tacio.operators.cron import Cron
from tacio.operators.executor import Agendador, Executor
from tacio.operators.manifesto import carregar_todos

RAIZ = Path(__file__).resolve().parents[1]


# --- cron ----------------------------------------------------------------------
def test_cron_basico():
    c = Cron("0 7 * * 1-6")
    assert c.bate(datetime(2026, 10, 5, 7, 0))       # segunda
    assert c.bate(datetime(2026, 10, 10, 7, 0))      # sábado
    assert not c.bate(datetime(2026, 10, 11, 7, 0))  # domingo
    assert not c.bate(datetime(2026, 10, 5, 7, 1))


def test_cron_passos_listas_e_domingo_7():
    assert Cron("*/15 8-18 * * *").bate(datetime(2026, 10, 5, 9, 45))
    assert not Cron("*/15 8-18 * * *").bate(datetime(2026, 10, 5, 19, 0))
    assert Cron("0 9 * * 7").bate(datetime(2026, 10, 11, 9, 0))  # domingo
    assert Cron("0 9 1,15 * *").bate(datetime(2026, 10, 15, 9, 0))
    with pytest.raises(ValueError):
        Cron("0 25 * * *")


# --- fakes -----------------------------------------------------------------------
class LeitorRuptura:
    """Simula o SQL de ruptura; `itens` controla o que o banco 'devolve'."""

    def __init__(self, itens):
        self.itens, self.chamadas = itens, []

    def consultar(self, sql, params=(), max_linhas=200):
        validar_somente_leitura(sql)
        self.chamadas.append((sql, list(params)))
        return [dict(i) for i in self.itens], False


class CanalMemoria:
    def __init__(self, nome, falhar=False):
        self.nome, self.falhar, self.msgs = nome, falhar, []

    def enviar(self, titulo, texto):
        if self.falhar:
            raise RuntimeError("fora do ar")
        self.msgs.append((titulo, texto))


def _item(emp, prod, status="RUPTURA", vlr="1000.00", disp=0):
    return {"CODEMP": emp, "CODPROD": prod, "DESCRPROD": f"PRODUTO {prod}",
            "DISPONIVEL": Decimal(disp), "VENDA_MEDIA_DIA": Decimal("2.50"),
            "COBERTURA_DIAS": Decimal("0"), "STATUS": status,
            "VLR_VENDIDO_PERIODO": Decimal(vlr)}


def _tenant(tmp_path, dir_ops):
    return TenantConfig(
        id="teste", nome="Teste", packs=["meta", "compras", "estoque"], codemp_permitidas=[],
        sql=SqlConfig(dsn="x"), politica=PoliticaConfig(),
        auditoria_arquivo=str(tmp_path / "audit.jsonl"), http=HttpConfig(),
        operators=OperatorsConfig(diretorio=str(dir_ops), estado=str(tmp_path / "ops.db")),
        canais={"console": {"tipo": "console"}},
    )


def _operator(dir_ops, **extra):
    dir_ops.mkdir(parents=True, exist_ok=True)
    linhas = [
        '[operator]', 'id = "rup"', 'nome = "Ruptura"', 'agenda = "0 7 * * *"',
        'tool = "ruptura_por_loja"', 'params = { dias = 30, incluir_risco = false }',
        'canais = ["mem"]', 'chave = ["CODEMP", "CODPROD"]',
    ] + [f"{k} = {json.dumps(v)}" for k, v in extra.items()] + [
        '[operator.minimo]', 'VLR_VENDIDO_PERIODO = 300',
    ]
    (dir_ops / "rup.toml").write_text("\n".join(linhas), encoding="utf-8")


def _executor(tmp_path, itens, canal=None, **extra):
    dir_ops = tmp_path / "ops"
    _operator(dir_ops, **extra)
    canal = canal or CanalMemoria("mem")
    leitor = LeitorRuptura(itens)
    ex = Executor(_tenant(tmp_path, dir_ops), sql=leitor, canais={"mem": canal})
    return ex, leitor, canal


# --- pack estoque --------------------------------------------------------------
def test_ruptura_sql_parametrizado(tmp_path):
    ex, leitor, _ = _executor(tmp_path, [_item(1, 10)])
    res = ex.tools["ruptura_por_loja"].fn(ex.ctx, dias=30, codemp=3)
    sql, params = leitor.chamadas[-1]
    assert "EST.TIPO = 'P'" in sql and "CAB.TIPMOV = 'V'" in sql
    assert params[1:] == [3, 3]  # CODEMP aplicado em vendas e estoque
    assert res["rupturas"] == 1


def test_ruptura_valida_limites(tmp_path):
    ex, _, _ = _executor(tmp_path, [])
    from tacio.policy import PoliticaViolada
    with pytest.raises(PoliticaViolada):
        ex.tools["ruptura_por_loja"].fn(ex.ctx, dias=2)


# --- operators -------------------------------------------------------------------
def test_manifestos_de_exemplo_sao_validos(tmp_path):
    ms = carregar_todos(RAIZ / "operators", {"ruptura_por_loja"}, {"console"})
    assert {m.id for m in ms} == {"ruptura_diaria", "risco_ruptura_semanal"}


def test_manifesto_com_tool_inexistente(tmp_path):
    d = tmp_path / "ops"
    d.mkdir()
    (d / "x.toml").write_text('[operator]\nid="x"\nagenda="* * * * *"\ntool="nao_existe"\n'
                              'canais=["console"]', encoding="utf-8")
    with pytest.raises(ValueError, match="nao_existe"):
        carregar_todos(d, {"ruptura_por_loja"}, {"console"})


def test_fluxo_novos_persistentes_resolvidos(tmp_path):
    itens = [_item(1, 10), _item(1, 11), _item(2, 10, vlr="50.00")]  # o 3º cai no mínimo
    ex, leitor, canal = _executor(tmp_path, itens)
    m = ex.manifestos()[0]

    r1 = ex.executar(m)
    assert (r1.novos, r1.resolvidos, r1.enviado) == (2, 0, True)
    assert "PRODUTO 10" in canal.msgs[-1][1] and "1.000,00" in canal.msgs[-1][1]

    r2 = ex.executar(m)  # nada mudou: não envia de novo
    assert (r2.novos, r2.persistentes, r2.enviado) == (0, 2, False)
    assert len(canal.msgs) == 1

    leitor.itens = [_item(1, 11), _item(3, 20)]  # 10 resolveu, 20 é novo
    r3 = ex.executar(m)
    assert (r3.novos, r3.resolvidos, r3.persistentes) == (1, 1, 1)
    assert "RESOLVIDOS" in canal.msgs[-1][1]


def test_simular_nao_envia_nem_grava(tmp_path):
    ex, _, canal = _executor(tmp_path, [_item(1, 10)])
    m = ex.manifestos()[0]
    r = ex.executar(m, simular=True)
    assert r.novos == 1 and not canal.msgs
    assert ex.executar(m).novos == 1  # estado não foi gravado na simulação


def test_falha_de_envio_nao_grava_estado(tmp_path):
    ex, _, _ = _executor(tmp_path, [_item(1, 10)], canal=CanalMemoria("mem", falhar=True))
    m = ex.manifestos()[0]
    r = ex.executar(m)
    assert r.erro and not r.enviado
    ex.canais["mem"] = CanalMemoria("mem")
    assert ex.executar(m).novos == 1  # reenvia na próxima


def test_llm_opcional_e_falha_nao_bloqueia(tmp_path, monkeypatch):
    ex, _, canal = _executor(tmp_path, [_item(1, 10)], analisar_com_llm=True)
    m = ex.manifestos()[0]
    monkeypatch.setattr(llm_mod, "analisar", lambda *a, **k: "Loja 1: repor PRODUTO 10 hoje.")
    ex.executar(m)
    assert canal.msgs[-1][1].startswith("ANÁLISE\nLoja 1")

    ex2, _, canal2 = _executor(tmp_path / "b", [_item(1, 10)], analisar_com_llm=True)
    def quebra(*a, **k):
        raise RuntimeError("sem chave")
    monkeypatch.setattr(llm_mod, "analisar", quebra)
    r = ex2.executar(ex2.manifestos()[0])
    assert r.enviado and "indisponível" in canal2.msgs[-1][1]


def test_agendador_nao_pula_minuto(tmp_path):
    ex, _, canal = _executor(tmp_path, [_item(1, 10)])
    ag = Agendador(ex)
    ag._ultimo = datetime(2026, 10, 5, 6, 58)
    assert ag.ciclo(datetime(2026, 10, 5, 7, 2, 30))  # 7:00 caiu entre ciclos
    assert ag.ciclo(datetime(2026, 10, 5, 7, 3)) == []  # não repete


def test_payload_teams_e_telegram(monkeypatch):
    enviados = []
    monkeypatch.setattr(canais_mod, "_post_json", lambda url, p, **k: enviados.append((url, p)))
    monkeypatch.setenv("URL_T", "https://exemplo/webhook")
    monkeypatch.setenv("TOK", "123:abc")
    canais_mod.Teams("t", {"url_env": "URL_T"}).enviar("Título", "corpo")
    canais_mod.Telegram("g", {"token_env": "TOK", "chat_id": -100}).enviar("T", "x" * 5000)
    card = enviados[0][1]["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert "bot123:abc/sendMessage" in enviados[1][0]
    assert len(enviados[1][1]["text"]) <= 4000
