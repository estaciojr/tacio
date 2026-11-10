import tomllib
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from tacio.adapters.sql_read import validar_somente_leitura
from tacio.pack import carregar_tools, montar_contexto
from tacio.policy import PoliticaViolada

from test_nucleo import _tenant

RAIZ = Path(__file__).resolve().parents[1]
PERIODO = {"data_inicio": "2026-10-01", "data_fim": "2026-10-31"}
REGRA = """
[entregas]
tops_remessa = [1154, 1108]
ocorrencias_entregue = [1]
ocorrencias_sam = [6]
tops_reposicao_sam = [1065]
tops_entrada_sam = [1219]
janela_dias = 90
dias_sem_baixa = 30
"""


class LeitorEntregas:
    def __init__(self):
        self.chamadas = []

    def consultar(self, sql, params=(), max_linhas=200):
        validar_somente_leitura(sql)
        self.chamadas.append((sql, list(params), max_linhas))
        linha = {"SITUACAO": "atrasada", "NOTAS": 2, "ITENS": 3, "VLR_PENDENTE": 10.126, "ITENS_SEM_CARGA": 1,
                 "ITENS_COM_SAM": 1, "QTD_ENTREGUE": 4, "VLR_ENTREGUE": 20.5,
                 "SITUACAO_REPOSICAO": "em_entrega", "SITUACAO_ENTRADA": "pendente", "SAMS": 5,
                 "VLR_REPOSICAO": 30.0, "VLR_AVARIADO": 40.0}
        return [linha], False


def _ctx(tmp_path, regra=REGRA):
    arq = tmp_path / "entregas.toml"
    arq.write_text(regra, encoding="utf-8")
    tenant = replace(_tenant(tmp_path), packs=["entregas"], regras={"entregas": str(arq)})
    leitor = LeitorEntregas()
    return montar_contexto(tenant, leitor), carregar_tools(tenant), leitor


def test_pendente_e_item_de_entrega_sem_baixa_e_com_saldo(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["entregas_pendentes"].fn(ctx)
    sql = leitor.chamadas[0][0]
    assert "CAB.CODTIPOPER IN (1108,1154)" in sql and "ITE.AD_ENTREGA = 'E'" in sql
    assert "NOT EXISTS (SELECT 1 FROM sankhya.TGFTEL T" in sql and "T.AD_CODOCOR IN (1)" in sql
    assert "ITE.QTDNEG - ISNULL(ITE.QTDCONFERIDA, 0) - ISNULL(ITE.QTDENTREGUE, 0)" in sql
    assert "DATEADD(DAY, -90," in sql and len(leitor.chamadas) == 1
    assert res["resumo"]["atrasada"] == {"notas": 2, "itens": 3, "vlr_pendente": 10.13, "itens_sem_carga": 1,
                                         "itens_com_sam": 1}
    assert res["janela_dias"] == 90 and res["dias_sem_baixa"] == 30


def test_sem_baixa_exige_nenhuma_ocorrencia_e_vem_antes_de_atrasada(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["entregas_pendentes"].fn(ctx)
    sql = leitor.chamadas[0][0]
    assert "OCR.TOTAL = 0 AND CAST(ITE.AD_DTENTREGA AS DATE) < DATEADD(DAY, -30," in sql
    assert sql.index("'sem_baixa'") < sql.index("'atrasada'")


def test_item_com_sam_continua_pendente_e_e_contado(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["entregas_pendentes"].fn(ctx)
    sql = leitor.chamadas[0][0]
    assert "T.AD_CODOCOR IN (6)" in sql and "SUM(P.COM_SAM)" in sql
    assert "NOT IN (6)" not in sql


@pytest.mark.parametrize("agrupar", ["dia", "empresa", "ocorrencia", "nota", "produto"])
def test_agrupamentos_pendentes_sao_sql_valido(tmp_path, agrupar):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["entregas_pendentes"].fn(ctx, agrupar=agrupar)
    assert "{" not in leitor.chamadas[1][0] and ("TOP 50" in leitor.chamadas[1][0]) == (agrupar != "dia")


def test_filtro_de_situacao_vem_depois_da_empresa(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["entregas_pendentes"].fn(ctx, codemp=1, agrupar="nota", situacao="atrasada")
    assert leitor.chamadas[1][1] == [1, "atrasada"] and "WHERE P.SITUACAO = ?" in leitor.chamadas[1][0]
    assert leitor.chamadas[0][1] == [1] and "P.SITUACAO = ?" not in leitor.chamadas[0][0]


def test_situacao_invalida_lista_validas(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="atrasada, hoje"):
        tools["entregas_pendentes"].fn(ctx, situacao="xyz")


@pytest.mark.parametrize("agrupar", ["dia", "mes", "empresa", "motorista", "produto"])
def test_realizadas_pela_primeira_baixa_no_periodo(tmp_path, agrupar):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["entregas_realizadas"].fn(ctx, **PERIODO, codemp=1, agrupar=agrupar)
    sql, params, _ = leitor.chamadas[0]
    assert "HAVING MIN(T.DHCHAMADA) >= ?" in sql and "{" not in sql
    ini, fim = date(2026, 10, 1), date(2026, 10, 31)
    assert params == [ini, fim, ini, fim, 1]
    assert res["resumo"]["vlr_entregue"] == 20.5


def test_realizadas_limita_periodo(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="92"):
        tools["entregas_realizadas"].fn(ctx, data_inicio="2026-01-01", data_fim="2026-10-31")


def test_sam_acompanha_as_duas_etapas(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["sam_entregas"].fn(ctx, **PERIODO)
    sql, params, _ = leitor.chamadas[0]
    assert "R.CODTIPOPER IN (1065)" in sql and "C.CODTIPOPER IN (1219)" in sql and "{" not in sql
    assert params == [date(2026, 10, 1), date(2026, 10, 1), date(2026, 10, 31), 1]
    assert "ISNULL(ORIGEM.CODEMP, R.CODEMP) AS CODEMP" in sql and "B.CODEMP IN (?)" in sql
    assert res["resumo"] == {"sams": 5, "reposicao": {"em_entrega": 5}, "entrada": {"pendente": 5},
                             "vlr_reposicao": 30.0, "vlr_avariado": 40.0}


@pytest.mark.parametrize("agrupar", ["mes", "empresa", "motorista", "produto", "sam"])
def test_agrupamentos_sam_sao_sql_valido(tmp_path, agrupar):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["sam_entregas"].fn(ctx, **PERIODO, agrupar=agrupar)
    assert "{" not in leitor.chamadas[1][0] and ("TOP 50" in leitor.chamadas[1][0]) == (agrupar != "mes")


def test_filtros_de_etapa_na_ordem_dos_parametros(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["sam_entregas"].fn(ctx, **PERIODO, agrupar="sam", reposicao="em_separacao", entrada="pendente")
    sql, params, _ = leitor.chamadas[1]
    assert params[-2:] == ["em_separacao", "pendente"]
    assert "WHERE S.SITUACAO_REPOSICAO = ? AND S.SITUACAO_ENTRADA = ?" in sql
    assert "S.SITUACAO_REPOSICAO = ?" not in leitor.chamadas[0][0]


@pytest.mark.parametrize("campo", ["reposicao", "entrada"])
def test_etapa_invalida_lista_validas(tmp_path, campo):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="Válidas"):
        tools["sam_entregas"].fn(ctx, **PERIODO, **{campo: "xyz"})


def test_sam_sem_tops_e_recusado(tmp_path):
    regra = "[entregas]\ntops_remessa = [1]\nocorrencias_entregue = [1]\nocorrencias_sam = [6]\n"
    ctx, tools, _ = _ctx(tmp_path, regra=regra)
    with pytest.raises(PoliticaViolada, match="tops_reposicao_sam"):
        tools["sam_entregas"].fn(ctx, **PERIODO)


def test_situacao_exige_filtro(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="nunota"):
        tools["situacao_entrega"].fn(ctx)


def test_situacao_por_nunota_ignora_janela(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["situacao_entrega"].fn(ctx, nunota=123)
    sql, params, _ = leitor.chamadas[0]
    assert params == [123, 1] and "DATEADD(DAY, -90" not in sql and res["janela_dias"] is None


def test_situacao_por_cliente_usa_janela_e_empresa(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["situacao_entrega"].fn(ctx, codparc=55, codemp=1)
    sql, params, _ = leitor.chamadas[0]
    assert params == [55, 1] and "DATEADD(DAY, -90" in sql
    assert sql.index("CAB.CODPARC = ?") < sql.index("CAB.CODEMP IN (?)")


def test_regra_sem_tops_e_recusada(tmp_path):
    ctx, tools, _ = _ctx(tmp_path, regra="[entregas]\nocorrencias_entregue = [1]\n")
    with pytest.raises(PoliticaViolada, match="tops_remessa"):
        tools["entregas_pendentes"].fn(ctx)


def test_regra_da_dalla():
    with open(RAIZ / "tenants" / "dalla.entregas.toml", "rb") as f:
        regra = tomllib.load(f)["entregas"]
    assert regra["tops_remessa"] == [1154] and regra["ocorrencias_entregue"] == [1]
