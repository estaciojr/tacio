from decimal import Decimal
from pathlib import Path

import pytest

from tacio.adapters.sql_read import validar_somente_leitura
from tacio.classificacao import carregar_classificacao
from tacio.pack import carregar_tools, montar_contexto
from tacio.policy import PoliticaViolada

from test_nucleo import TOPS_TESTE, _tenant

RAIZ = Path(__file__).resolve().parents[1]


class LeitorCompras:
    """Devolve o movimento por TOP para a checagem e uma linha qualquer para o resto."""

    def __init__(self, movimento=None):
        self.chamadas = []
        self.movimento = movimento or []

    def consultar(self, sql, params=(), max_linhas=200):
        validar_somente_leitura(sql)
        self.chamadas.append((sql, list(params)))
        if "NOTAS_PARC_GRUPO" in sql:
            return self.movimento, False
        return [{"CODPARC": 10, "NOMEPARC": "FORNECEDOR", "VLR_COMPRADO": Decimal("100"),
                 "QTD_NOTAS": 1, "CODTIPOPER": 1410}], False


def _ctx(tmp_path, movimento=None):
    leitor = LeitorCompras(movimento)
    tenant = _tenant(tmp_path)
    return montar_contexto(tenant, leitor), carregar_tools(tenant), leitor


def _mov(top, notas, vlr, grupo=0):
    return {"CODTIPOPER": top, "QTD_NOTAS": notas, "VLR_NOTAS": Decimal(vlr), "NOTAS_PARC_GRUPO": grupo}


# --- arquivo de classificação --------------------------------------------------
def test_classificacao_da_dalla_e_valida():
    cl = carregar_classificacao(RAIZ / "tenants" / "dalla.tops.toml")
    assert cl.validar(None) == ["revenda"]
    assert {1430, 1410} <= set(cl.tops_de(["revenda"]))
    assert 1456 in cl.tops_de(["transferencia"]) and 1456 not in cl.tops_de(["revenda"])
    assert cl.tops_negativas(cl.tops_de(["devolucao_compra"]))  # devolução subtrai


def test_classificacao_rejeita_natureza_inexistente(tmp_path):
    arq = tmp_path / "t.toml"
    arq.write_text(TOPS_TESTE + '1999 = "nao_existe"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="1999"):
        carregar_classificacao(arq)


def test_natureza_desconhecida_vira_mensagem(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="Válidas"):
        tools["ultimas_compras_produto"].fn(ctx, codprod=1, naturezas=["xyz"])


# --- tools ---------------------------------------------------------------------
def test_padrao_e_so_revenda(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["compras_por_fornecedor"].fn(ctx, data_inicio="2026-09-01", data_fim="2026-09-30")
    sql, params = leitor.chamadas[0]
    assert "CODTIPOPER IN (1410)" in sql and "1456" not in sql
    assert "TIPMOV" not in sql  # a regra vem da classificação, não do TIPMOV
    assert res["naturezas"] == ["revenda"]


def test_devolucao_subtrai(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["compras_por_fornecedor"].fn(ctx, data_inicio="2026-09-01", data_fim="2026-09-30",
                                       naturezas=["revenda", "devolucao_compra"])
    sql, _ = leitor.chamadas[0]
    assert "CASE WHEN CAB.CODTIPOPER IN (1500) THEN -1 ELSE 1 END" in sql


def test_avisos_de_classificacao(tmp_path):
    movimento = [
        _mov(1410, 10, "1000", grupo=2),   # revenda com parceiro do grupo
        _mov(1456, 5, "500", grupo=4),     # transferência com parceiro de fora
        _mov(1777, 3, "300"),              # TOP não classificada
    ]
    ctx, tools, _ = _ctx(tmp_path, movimento)
    res = tools["entradas_por_natureza"].fn(ctx, data_inicio="2026-09-01", data_fim="2026-09-30")
    avisos = " | ".join(res["avisos"])
    assert "TOP 1777 não classificada" in avisos
    assert "TOP 1410 (revenda): 2 de 10" in avisos
    assert "TOP 1456 (transferência): 1 de 5" in avisos


def test_entradas_por_natureza_soma_com_sinal(tmp_path):
    movimento = [_mov(1410, 2, "1000"), _mov(1456, 3, "400"), _mov(1500, 1, "150")]
    ctx, tools, _ = _ctx(tmp_path, movimento)
    res = tools["entradas_por_natureza"].fn(ctx, data_inicio="2026-09-01", data_fim="2026-09-30")
    por_nat = {l["NATUREZA"]: l for l in res["linhas"]}
    assert por_nat["revenda"]["VLR_NOTAS"] == 1000 and por_nat["revenda"]["CONTA_COMO_COMPRA"]
    assert por_nat["transferencia"]["VLR_NOTAS"] == 400
    assert por_nat["devolucao_compra"]["VLR_NOTAS"] == -150


def test_ultimas_compras_marca_natureza(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    res = tools["ultimas_compras_produto"].fn(ctx, codprod=5418)
    assert res["linhas"][0]["NATUREZA"] == "revenda"


def test_sem_classificacao_avisa_o_agente(tmp_path):
    tenant = _tenant(tmp_path)
    tenant.classificacao_tops = None
    ctx = montar_contexto(tenant, LeitorCompras())
    with pytest.raises(PoliticaViolada, match="não configurada"):
        carregar_tools(tenant)["naturezas_compra"].fn(ctx)


def test_parceiro_do_grupo_fornecedor_nao_gera_aviso(tmp_path):
    # 3 notas de revenda com parceiro do grupo, 2 delas de um fornecedor do grupo liberado
    movimento = [dict(_mov(1410, 10, "1000", grupo=3), NOTAS_GRUPO_FORNECEDOR=2)]
    ctx, tools, leitor = _ctx(tmp_path, movimento)
    ctx.classificacao.checagens.parceiros_grupo_fornecedores = [575577]
    res = tools["entradas_por_natureza"].fn(ctx, data_inicio="2026-09-01", data_fim="2026-09-30")
    assert "CAB.CODPARC IN (575577)" in leitor.chamadas[0][0]
    assert res["avisos"] == ["TOP 1410 (revenda): 1 de 10 notas têm parceiro que é empresa do grupo."]


def test_dalla_libera_vero_decor():
    cl = carregar_classificacao(RAIZ / "tenants" / "dalla.tops.toml")
    assert 575577 in cl.checagens.parceiros_grupo_fornecedores


def test_campo_da_top_diverge_da_classificacao(tmp_path):
    # TOPFLAG_0 = notas com BONIFICACAO='S' na TOP; TOPFLAG_1 = AD_TRANSFERENCIA='S'
    movimento = [
        dict(_mov(1410, 4, "100"), TOPFLAG_0=1, TOPFLAG_1=0),  # revenda, mas TOP diz bonificação
        dict(_mov(1456, 5, "100", grupo=5), TOPFLAG_0=0, TOPFLAG_1=3),  # transferência sem flag em 2
    ]
    ctx, tools, leitor = _ctx(tmp_path, movimento)
    ctx.classificacao.checagens.campos_top = {"bonificacao": "BONIFICACAO",
                                              "transferencia": "AD_TRANSFERENCIA"}
    res = tools["entradas_por_natureza"].fn(ctx, data_inicio="2026-09-01", data_fim="2026-09-30")
    sql = leitor.chamadas[0][0]
    assert "TPO.DHALTER = CAB.DHTIPOPER" in sql and "TPO.BONIFICACAO = 'S'" in sql
    avisos = " | ".join(res["avisos"])
    assert "TOP 1410 classificada como revenda, mas BONIFICACAO = 'S' na TOP em 1 de 4" in avisos
    assert "TOP 1456 classificada como transferencia, mas AD_TRANSFERENCIA = 'N' na TOP em 2 de 5" in avisos


def test_dalla_liga_campos_da_top():
    cl = carregar_classificacao(RAIZ / "tenants" / "dalla.tops.toml")
    assert cl.checagens.campos_top == {"bonificacao": "BONIFICACAO", "transferencia": "AD_TRANSFERENCIA"}
    assert cl.natureza_da_top(1421) == "outras" and cl.natureza_da_top(1431) == "revenda"
