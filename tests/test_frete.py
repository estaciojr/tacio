from dataclasses import replace
from datetime import date

import pytest

from tacio.adapters.sql_read import validar_somente_leitura
from tacio.pack import carregar_tools, montar_contexto
from tacio.policy import PoliticaViolada

from test_nucleo import _tenant

PERIODO = {"data_inicio": "2026-10-01", "data_fim": "2026-10-31"}
INI, FIM = date(2026, 10, 1), date(2026, 10, 31)

REGRA = """
[frete]
tops_remessa = [1154]
tops_venda = [1117]
tops_venda_direta = [1108]
tops_devolucao = [1203, 1207]
empresa_transportadora = 7
tops_transportadora = [1120, 1125]
tops_cte = [1606]
tops_cte_ecommerce = [1611]
parceiros_internos = [7]
naturezas_combustivel = [2011006]
naturezas_frota_sem_placa = [2030707]
tipos_veiculo = ["C", "R"]
veiculos_excluidos = [71]
"""


class LeitorFrete:
    def __init__(self):
        self.chamadas = []

    def consultar(self, sql, params=(), max_linhas=200):
        validar_somente_leitura(sql)
        self.chamadas.append((sql, list(params), max_linhas))
        linha = {"MES": "2026-10", "NOTAS": 3, "VLR_COBRADO": 100.004, "VLR_DEVOLVIDO": 10, "VLR_LIQUIDO": 90,
                 "DOCUMENTOS": 2, "VLR_EMITIDO": 80, "REMESSAS": 4, "VLR_FRETE": 50, "SEM_DOCUMENTO": 1,
                 "VLR_SEM_DOCUMENTO": 5, "ORIGEM": "compras", "TITULOS": 2, "VLR_EM_ABERTO": 20, "CTES": 1, "VLR": 7,
                 "CARGAS": 2, "PESO_KG": 1000, "CODVEICULO": 20, "PLACA": "ABC1D23", "VLR_COMBUSTIVEL": 10,
                 "VLR_MANUTENCAO": 5, "VLR_AGREGADO": 0, "VLR_CUSTO": 15, "KM_INICIAL": 1000, "KM_FINAL": 1300,
                 "KM_RODADO": 300, "CODNAT": 2030707, "NATUREZA": "IPVA ", "KM_ULTIMO": 1300}
        return [linha], False


def _ctx(tmp_path, regra=REGRA, **tenant):
    arq = tmp_path / "frete.toml"
    arq.write_text(regra, encoding="utf-8")
    t = replace(_tenant(tmp_path), packs=["frete"], regras={"frete": str(arq)}, **tenant)
    leitor = LeitorFrete()
    return montar_contexto(t, leitor), carregar_tools(t), leitor


def test_arrecadado_pela_remessa_desconta_devolucao(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["frete_arrecadado"].fn(ctx, **PERIODO)
    sql, params, _ = leitor.chamadas[0]
    assert "CAB.CODTIPOPER IN (1108,1154,1203,1207)" in sql and "1117" not in sql
    assert "WHEN CAB.CODTIPOPER IN (1203,1207) THEN -1" in sql and params == [INI, FIM, 1]
    assert res["resumo"] == {"notas": 3, "vlr_cobrado": 100.0, "vlr_devolvido": 10.0, "vlr_liquido": 90.0}


def test_arrecadado_pela_venda_nao_soma_remessa(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["frete_arrecadado"].fn(ctx, **PERIODO, base="venda")
    assert "CAB.CODTIPOPER IN (1108,1117,1203,1207)" in leitor.chamadas[0][0]


def test_base_invalida(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="remessa, venda"):
        tools["frete_arrecadado"].fn(ctx, **PERIODO, base="nota")


@pytest.mark.parametrize("agrupar", ["mes", "dia", "empresa", "operacao"])
def test_agrupamentos_arrecadado(tmp_path, agrupar):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["frete_arrecadado"].fn(ctx, **PERIODO, agrupar=agrupar)
    assert "{" not in leitor.chamadas[0][0]


def test_transportadora_concilia_com_remessas(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path, codemp_permitidas=[])
    res = tools["frete_transportadora"].fn(ctx, **PERIODO, agrupar="loja", codemp=1)
    sql, params, _ = leitor.chamadas[0]
    assert "CAB.CODEMP = 7 AND CAB.CODTIPOPER IN (1120,1125)" in sql and "LJ.CODEMP" in sql and params == [INI, FIM, 1]
    assert "R.AD_NUNOTACTE IS NULL" in leitor.chamadas[-1][0] and "R.CODTIPOPER IN (1154)" in leitor.chamadas[-1][0]
    assert res["conciliacao"]["remessas_sem_documento"] == 1 and res["resumo"]["vlr_emitido"] == 80.0


def test_transportadora_respeita_empresas_permitidas(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)  # tenant de teste só libera a empresa 1: a transportadora (7) fica fora
    with pytest.raises(PoliticaViolada, match="Empresa 7"):
        tools["frete_transportadora"].fn(ctx, **PERIODO)


def test_pago_tira_parceiro_interno_e_mostra_pendentes(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["frete_pago"].fn(ctx, **PERIODO)
    sql = leitor.chamadas[0][0]
    assert "F.CODTIPOPER IN (1606,1611)" in sql and "F.CODPARC NOT IN (7)" in sql and "F.PROVISAO = 'N'" in sql
    assert "sankhya.TGFIXN" in leitor.chamadas[-1][0] and "X.STATUS = 0" in leitor.chamadas[-1][0]
    assert res["resumo"]["pendentes"] == {"ctes": 1, "vlr": 7.0}


def test_pago_so_ecommerce(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["frete_pago"].fn(ctx, **PERIODO, origem="ecommerce", agrupar="transportadora")
    assert "F.CODTIPOPER IN (1611)" in leitor.chamadas[0][0]


def test_pago_origem_invalida(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="compras, ecommerce"):
        tools["frete_pago"].fn(ctx, **PERIODO, origem="cd")


@pytest.mark.parametrize("agrupar", ["dia", "veiculo", "motorista", "carga"])
def test_agrupamentos_cargas(tmp_path, agrupar):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["cargas"].fn(ctx, **PERIODO, agrupar=agrupar)
    sql = leitor.chamadas[0][0]
    assert "{" not in sql and "CAB.ORDEMCARGA > 0" in sql and ("TOP 50" in sql) == (agrupar != "dia")


def test_cargas_limita_periodo(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="92"):
        tools["cargas"].fn(ctx, data_inicio="2026-01-01", data_fim="2026-06-30")


def test_custo_por_placa_so_frota_de_entrega(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path, codemp_permitidas=[])
    res = tools["custo_por_placa"].fn(ctx, **PERIODO)
    sql, params, _ = leitor.chamadas[0]
    assert "V.TIPO IN ('C','R') AND V.CODVEICULO NOT IN (71)" in sql and params == [INI, FIM] * 5
    assert "CODNAT IN (2011006)" in sql and "sankhya.TGFRAV" in sql and "AD_ORDPAGMOT" in sql
    l = res["linhas"][0]
    assert l["PERC_CUSTO_SOBRE_FRETE"] == 30.0 and l["CUSTO_POR_KM"] == 0.05
    assert res["resumo"]["sem_placa"]["por_natureza"][0]["natureza"] == "IPVA"


def test_custo_de_um_veiculo_ignora_filtro_de_frota(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path, codemp_permitidas=[])
    res = tools["custo_por_placa"].fn(ctx, **PERIODO, codveiculo=8)
    sql, params, _ = leitor.chamadas[0]
    assert "V.TIPO IN" not in sql and params[-1] == 8 and len(leitor.chamadas) == 1
    assert res["resumo"]["sem_placa"]["vlr"] == 0.0


def test_custo_por_placa_recusa_tenant_com_empresas_restritas(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="frota do grupo"):
        tools["custo_por_placa"].fn(ctx, **PERIODO)


def test_frota_incluir_todos(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["frota"].fn(ctx)
    tools["frota"].fn(ctx, incluir_todos=True)
    assert "V.TIPO IN ('C','R')" in leitor.chamadas[0][0] and "V.TIPO IN" not in leitor.chamadas[1][0]


def test_tipo_de_veiculo_invalido_na_regra(tmp_path):
    ctx, tools, _ = _ctx(tmp_path, regra=REGRA.replace('["C", "R"]', '["C\'; --"]'))
    with pytest.raises(PoliticaViolada, match="tipos_veiculo"):
        tools["frota"].fn(ctx)


def test_regra_incompleta(tmp_path):
    ctx, tools, _ = _ctx(tmp_path, regra="[frete]\ntops_remessa = [1154]\n")
    with pytest.raises(PoliticaViolada, match="incompleta"):
        tools["frete_arrecadado"].fn(ctx, **PERIODO)


def test_custo_por_placa_mes_a_mes(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path, codemp_permitidas=[])
    res = tools["custo_por_placa"].fn(ctx, data_inicio="2026-08-15", data_fim="2026-10-31", agrupar="mes")
    fatias = [c[1][:2] for c in leitor.chamadas if "sankhya.TGFRAV" in c[0]]
    assert fatias == [[date(2026, 8, 15), date(2026, 8, 31)], [date(2026, 9, 1), date(2026, 9, 30)],
                      [date(2026, 10, 1), date(2026, 10, 31)]]
    assert [l["MES"] for l in res["linhas"]] == ["2026-08", "2026-09", "2026-10"]
    assert res["resumo"]["veiculos"] == 1 and len(res["resumo"]["por_mes"]) == 3
    assert res["resumo"]["vlr_custo"] == 45.0 and res["agrupar"] == "mes"


def test_custo_por_placa_agrupar_invalido(tmp_path):
    ctx, tools, _ = _ctx(tmp_path, codemp_permitidas=[])
    with pytest.raises(PoliticaViolada, match="Válidos"):
        tools["custo_por_placa"].fn(ctx, **PERIODO, agrupar="dia")
