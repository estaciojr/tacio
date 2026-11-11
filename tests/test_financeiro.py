from dataclasses import replace
from datetime import date

import pytest

from tacio.adapters.sql_read import validar_somente_leitura
from tacio.pack import carregar_tools, montar_contexto
from tacio.policy import PoliticaViolada

from test_nucleo import _tenant

PERIODO = {"data_inicio": "2026-10-01", "data_fim": "2026-10-31"}
INI, FIM = date(2026, 10, 1), date(2026, 10, 31)


class LeitorFinanceiro:
    def __init__(self):
        self.chamadas = []

    def consultar(self, sql, params=(), max_linhas=200):
        validar_somente_leitura(sql)
        self.chamadas.append((sql, list(params), max_linhas))
        linha = {"FAIXA": "vencido_1_30", "TITULOS": 2, "VLR_ABERTO": 100.004, "VLR_VENCIDO": 60, "VLR_A_VENCER": 40,
                 "VLR_VENCIDO_MAIS_90": 0, "VLR_BAIXA": 10, "VLR_CAIXA": 70.5, "VLR_COMPENSADO": 30,
                 "TITULOS_CAIXA": 7, "TITULOS_COMPENSADOS": 3, "VLR_RECEBER": 90, "VLR_PAGAR": 30, "PERIODO": "2026-10-01",
                 "CLASSE": "C", "CONTAS": 4, "VLR_SALDO": 12.345}
        return [linha], False


REGRA = """
[financeiro.segmentos.adiantamento]
nome = "Adiantamento"
tipo = "receber"
naturezas = [2020510]
"""


def _ctx(tmp_path, regra=None):
    regras = {}
    if regra:
        arq = tmp_path / "financeiro.toml"
        arq.write_text(regra, encoding="utf-8")
        regras = {"financeiro": str(arq)}
    tenant = replace(_tenant(tmp_path), packs=["financeiro"], regras=regras)
    leitor = LeitorFinanceiro()
    return montar_contexto(tenant, leitor), carregar_tools(tenant), leitor


def test_aberto_usa_saldo_do_titulo_sem_provisao(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["contas_em_aberto"].fn(ctx, tipo="receber")
    sql, params, _ = leitor.chamadas[0]
    assert "F.RECDESP = ? AND F.DHBAIXA IS NULL AND F.PROVISAO = 'N'" in sql and params == [1, 1]
    assert "ISNULL(F.VLRDESDOB, 0)" in sql and "sankhya.TGFIMF" in sql
    assert len(leitor.chamadas) == 1 and res["resumo"]["vlr_aberto"] == 100.0
    assert res["resumo"]["por_faixa"] == {"vencido_1_30": {"titulos": 2, "vlr": 100.0}}


def test_pagar_inverte_o_sinal_do_filtro(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["contas_em_aberto"].fn(ctx, tipo="pagar")
    assert leitor.chamadas[0][1][0] == -1


def test_tipo_invalido_lista_validos(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="receber, pagar"):
        tools["contas_em_aberto"].fn(ctx, tipo="xyz")


@pytest.mark.parametrize("agrupar", ["empresa", "natureza", "parceiro", "operacao", "tipo_titulo", "vencimento", "titulo"])
def test_agrupamentos_aberto_sao_sql_valido(tmp_path, agrupar):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["contas_em_aberto"].fn(ctx, tipo="pagar", agrupar=agrupar)
    sql = leitor.chamadas[1][0]
    assert "{" not in sql and ("TOP 50" in sql) == (agrupar != "vencimento")


def test_situacao_e_filtros_do_titulo(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["contas_em_aberto"].fn(ctx, tipo="receber", agrupar="titulo", situacao="vencido", codparc=9, codnat=5)
    sql, params, _ = leitor.chamadas[1]
    assert params == [1, 1, 9, 5] and "WHERE A.DIAS_ATRASO > 0" in sql
    assert "A.DIAS_ATRASO > 0" not in leitor.chamadas[0][0].split("GROUP BY")[0].split(") ")[-1]


def test_pergunta_generica_deixa_segmento_fora(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path, REGRA)
    res = tools["contas_em_aberto"].fn(ctx, tipo="receber")
    assert "AND NOT (F.RECDESP = 1 AND F.CODNAT IN (2020510))" in leitor.chamadas[0][0]
    assert res["segmento"] is None


def test_segmento_traz_somente_ele(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path, REGRA)
    res = tools["contas_em_aberto"].fn(ctx, tipo="receber", segmento="adiantamento")
    sql = leitor.chamadas[0][0]
    assert "AND F.RECDESP = 1 AND F.CODNAT IN (2020510)" in sql and "NOT (" not in sql
    assert res["segmento"] == "adiantamento"


@pytest.mark.parametrize("filtro", [{"codparc": 7}, {"codnat": 2020510}])
def test_filtrar_parceiro_ou_natureza_traz_tudo(tmp_path, filtro):
    ctx, tools, leitor = _ctx(tmp_path, REGRA)
    tools["contas_em_aberto"].fn(ctx, tipo="receber", **filtro)
    assert "NOT (" not in leitor.chamadas[0][0]


def test_segmento_de_outro_tipo_e_recusado(tmp_path):
    ctx, tools, _ = _ctx(tmp_path, REGRA)
    with pytest.raises(PoliticaViolada, match="pagar"):
        tools["contas_em_aberto"].fn(ctx, tipo="pagar", segmento="adiantamento")


def test_fluxo_deixa_segmento_fora(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path, REGRA)
    tools["fluxo_previsto"].fn(ctx, **PERIODO)
    assert all("AND NOT (F.RECDESP = 1 AND F.CODNAT IN (2020510))" in c[0] for c in leitor.chamadas)


def test_sem_regra_nao_ha_segmentos(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    assert tools["segmentos_financeiro"].fn(ctx)["linhas"] == []
    tools["contas_em_aberto"].fn(ctx, tipo="receber")
    assert "NOT (" not in leitor.chamadas[0][0]


def test_baixas_por_padrao_so_o_que_movimentou_caixa(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["baixas_periodo"].fn(ctx, tipo="pagar", **PERIODO)
    sql, params, _ = leitor.chamadas[0]
    assert "F.NUBCO IS NOT NULL" in sql and params == [-1, INI, FIM, 1]
    assert res["resumo"] == {"vlr_caixa": 70.5, "vlr_compensado": 30.0, "titulos_caixa": 7, "titulos_compensados": 3}


def test_baixas_com_compensacoes(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["baixas_periodo"].fn(ctx, tipo="receber", **PERIODO, incluir_compensacoes=True)
    assert "F.NUBCO IS NOT NULL" not in leitor.chamadas[0][0]


@pytest.mark.parametrize("agrupar", ["dia", "mes", "empresa", "natureza", "parceiro", "conta", "operacao"])
def test_agrupamentos_baixa_sao_sql_valido(tmp_path, agrupar):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["baixas_periodo"].fn(ctx, tipo="receber", **PERIODO, agrupar=agrupar)
    sql = leitor.chamadas[0][0]
    assert "{" not in sql and "sankhya.TGFFIN F" in sql and ("TOP 50" in sql) == (agrupar not in ("dia", "mes"))


def test_baixas_limita_periodo(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="366"):
        tools["baixas_periodo"].fn(ctx, tipo="pagar", data_inicio="2024-01-01", data_fim="2026-01-01")


def test_fluxo_acumula_e_mostra_vencidos_de_fora(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["fluxo_previsto"].fn(ctx, **PERIODO)
    sql, params, _ = leitor.chamadas[0]
    assert "F.PROVISAO = 'N'" in sql and params == [1, INI, FIM]
    assert res["linhas"][0]["VLR_SALDO"] == 60.0 and res["linhas"][0]["VLR_ACUMULADO"] == 60.0
    assert res["resumo"]["vencidos_antes"] == {"vlr_receber": 90.0, "vlr_pagar": 30.0}


def test_fluxo_com_provisao(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["fluxo_previsto"].fn(ctx, **PERIODO, incluir_provisao=True, agrupar="semana")
    assert "PROVISAO" not in leitor.chamadas[0][0] and "F.PROVISAO = 'N'" in leitor.chamadas[1][0]


def test_saldo_abertura_mais_movimento(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["saldo_contas"].fn(ctx, classe="C")
    sql, params, _ = leitor.chamadas[0]
    assert "sankhya.TGFSBC" in sql and "B.VLRLANC * B.RECDESP" in sql and "C.ATIVA = 'S'" in sql
    assert params == [1, "C"] and res["resumo"] == {"C": {"contas": 4, "vlr_saldo": 12.35}}


def test_classe_invalida(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="classe"):
        tools["saldo_contas"].fn(ctx, classe="Q")
