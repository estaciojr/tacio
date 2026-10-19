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


# --- pedidos_pendentes -----------------------------------------------------------
class LeitorPedidos:
    """Responde cada consulta de pedidos_pendentes pelo trecho que a identifica."""

    def __init__(self, resumo=None, aguardando=None, nao_classificadas=None):
        self.chamadas = []
        self.resumo = resumo or []
        self.aguardando = aguardando or []
        self.nao_classificadas = nao_classificadas or []

    def consultar(self, sql, params=(), max_linhas=200):
        validar_somente_leitura(sql)
        self.chamadas.append((sql, list(params)))
        if "GROUP BY B.SITUACAO_PEDIDO" in sql:
            return self.resumo, False
        if "DIAS_AGUARDANDO" in sql:
            return self.aguardando, False
        if "WITH ITENS" not in sql:
            return self.nao_classificadas, False
        return [{"NUNOTA": 1, "CODTIPOPER": 1310, "VLR_SALDO": Decimal("10.456")}], False


def _ctx_pedidos(tmp_path, **kw):
    leitor = LeitorPedidos(**kw)
    tenant = _tenant(tmp_path)
    return montar_contexto(tenant, leitor), carregar_tools(tenant), leitor


def _sit(situacao, pedidos, vlr, **extra):
    return dict(SITUACAO_PEDIDO=situacao, PEDIDOS=pedidos, ITENS=pedidos * 2,
                VLR_SALDO=Decimal(vlr), **extra)


def test_dalla_classifica_pedidos():
    cl = carregar_classificacao(RAIZ / "tenants" / "dalla.tops.toml")
    assert cl.tops_pedido_de(["revenda"]) == [1310, 1311]
    assert cl.natureza_do_pedido(1324) == "encomenda" and cl.natureza_do_pedido(1379) == "sellers"
    assert cl.natureza_do_pedido(1304) == "orcamento"
    assert cl.natureza_da_top(1479) == "sellers" and 1479 not in cl.tops_de(["revenda"])
    # pedido não pode vazar para as tools de nota
    assert not {1310, 1311, 1324, 1379} & set(cl.tops_de(list(cl.naturezas)))


def test_pedidos_padrao_revenda_so_confirmados(tmp_path):
    ctx, tools, leitor = _ctx_pedidos(tmp_path)
    res = tools["pedidos_pendentes"].fn(ctx, codparc=99)
    sql, params = leitor.chamadas[0]
    assert "CAB.CODTIPOPER IN (1310)" in sql and "1305" not in sql
    assert "B.SITUACAO_PEDIDO = 'confirmado'" in sql
    assert "CAB.CODEMP IN (?)" in sql and "CAB.CODPARC = ?" in sql and params == [1, 99]
    assert res["agrupar"] == "pedido" and res["naturezas"] == ["revenda"]
    assert res["linhas"][0]["NATUREZA"] == "revenda" and res["linhas"][0]["VLR_SALDO"] == 10.46


def test_pedidos_com_produto_detalha_por_item(tmp_path):
    ctx, tools, leitor = _ctx_pedidos(tmp_path)
    res = tools["pedidos_pendentes"].fn(ctx, codprod=5418, situacao="atrasados")
    sql, params = leitor.chamadas[0]
    assert res["agrupar"] == "item" and "B.SEQUENCIA" in sql and "B.DIAS_ATRASO > 0" in sql
    assert "ITE.CODPROD = ?" in sql and params[-1] == 5418


def test_pedidos_parametros_invalidos(tmp_path):
    ctx, tools, _ = _ctx_pedidos(tmp_path)
    with pytest.raises(PoliticaViolada, match="situacao"):
        tools["pedidos_pendentes"].fn(ctx, situacao="xyz")
    with pytest.raises(PoliticaViolada, match="agrupar"):
        tools["pedidos_pendentes"].fn(ctx, agrupar="xyz")
    with pytest.raises(PoliticaViolada, match="Nenhuma TOP de pedido"):
        tools["pedidos_pendentes"].fn(ctx, naturezas=["devolucao_compra"])


def test_pedidos_nao_confirmados_ficam_fora_da_conta(tmp_path):
    resumo = [
        _sit("confirmado", 10, "1000", VLR_ATRASADO=Decimal("300"), PEDIDOS_ATRASADOS=3,
             VLR_EM_RECEBIMENTO=Decimal("50"), PEDIDOS_SEM_PREVISAO=2, PEDIDOS_RECEBIMENTO_PARADO=1),
        _sit("aguardando_autorizacao", 4, "523488.26"),
        _sit("nao_confirmado", 2, "80"),
    ]
    aguardando = [{"NUNOTA": 7, "VLR_SALDO": Decimal("1.005"), "SOLICITANTE": "RONEI.MAIA"}]
    ctx, tools, _ = _ctx_pedidos(tmp_path, resumo=resumo, aguardando=aguardando)
    res = tools["pedidos_pendentes"].fn(ctx)
    assert res["resumo"] == {"pedidos": 10, "itens": 20, "vlr_saldo": 1000.0, "vlr_atrasado": 300.0,
                             "pedidos_atrasados": 3, "vlr_em_recebimento": 50.0}
    assert res["aguardando_autorizacao"][0]["SOLICITANTE"] == "RONEI.MAIA"
    avisos = " | ".join(res["avisos"])
    assert "4 pedidos aguardando autorização (R$ 523.488,26)" in avisos
    assert "2 pedidos não confirmados e sem pedido de liberação (R$ 80,00)" in avisos
    assert "2 pedidos sem previsão de entrega" in avisos
    assert "1 pedidos com nota de entrada digitada há mais de 30 dias" in avisos


def test_pedido_de_top_nao_classificada_avisa(tmp_path):
    ctx, tools, leitor = _ctx_pedidos(tmp_path, nao_classificadas=[{"CODTIPOPER": 1399, "PEDIDOS": 5}])
    res = tools["pedidos_pendentes"].fn(ctx)
    sql = leitor.chamadas[-1][0]
    assert "NOT IN (1305,1310)" in sql
    assert "TOP de pedido 1399 não classificada: 5 pedidos" in " | ".join(res["avisos"])


def test_naturezas_compra_mostra_tops_de_pedido(tmp_path):
    ctx, tools, _ = _ctx_pedidos(tmp_path)
    por_nat = {l["NATUREZA"]: l for l in tools["naturezas_compra"].fn(ctx)["linhas"]}
    assert por_nat["revenda"]["TOPS"] == "1410" and por_nat["revenda"]["TOPS_PEDIDO"] == "1310"
