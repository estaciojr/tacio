import tomllib
from datetime import date
from dataclasses import replace
from pathlib import Path

import pytest

from tacio.adapters.sql_read import aplicar_schema, validar_somente_leitura
from tacio.config import SqlConfig
from tacio.pack import carregar_tools, montar_contexto
from tacio.policy import PoliticaViolada

from test_nucleo import _tenant

RAIZ = Path(__file__).resolve().parents[1]
PERIODO = {"data_inicio": "2026-10-01", "data_fim": "2026-10-31"}


REGRA = """
[vendas.segmentos.parceira]
nome = "Empresa parceira"
tops = [1116]
empresas = [1, 25]
[vendas.segmentos.servicos]
nome = "Serviços"
tops = [1125, 1119]
"""


class LeitorVendas:
    def __init__(self):
        self.chamadas = []

    def consultar(self, sql, params=(), max_linhas=200):
        validar_somente_leitura(sql)
        self.chamadas.append((sql, list(params), max_linhas))
        linha = {"VLR_VENDA": 1000.004, "VLR_DEVOLUCAO": 100, "VLR_LIQUIDO": 900.004, "NOTAS_VENDA": 4,
                 "NOTAS_DEVOLUCAO": 1, "NOTAS": 5, "QTD_LIQUIDA": 7}
        return [linha], False


def _ctx(tmp_path, regra=REGRA):
    arq = tmp_path / "vendas.toml"
    arq.write_text(regra, encoding="utf-8")
    tenant = replace(_tenant(tmp_path), packs=["vendas"], regras={"vendas": str(arq)})
    leitor = LeitorVendas()
    return montar_contexto(tenant, leitor), carregar_tools(tenant), leitor


def test_universo_de_venda(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["vendas_resumo"].fn(ctx, **PERIODO)
    sql, params, _ = leitor.chamadas[0]
    assert "TPO.DHALTER = CAB.DHTIPOPER" in sql and "MAX(" not in sql
    assert "TPO.GOLSINAL = -1" in sql and "CAB.STATUSNOTA = 'L'" in sql
    assert "TPO.GOLSINAL * TPO.GOLDEV * -1" in sql  # devolução subtrai
    assert params[-1] == 1  # escopo de empresas do tenant aplicado
    assert res["linhas"][0]["VLR_VENDA"] == 1000.0 and res["linhas"][0]["TICKET_MEDIO"] == 250.0
    assert res["resumo"]["vlr_liquido"] == 900.0


def test_pergunta_generica_deixa_segmentos_fora(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["vendas_resumo"].fn(ctx, **PERIODO)
    assert all("CAB.CODTIPOPER NOT IN (1116,1119,1125)" in c[0] for c in leitor.chamadas)
    assert all("CAB.CODEMP NOT IN (1,25)" in c[0] for c in leitor.chamadas)
    assert res["segmento"] is None and "avisos" not in res


def test_segmento_traz_somente_ele(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["vendas_resumo"].fn(ctx, **PERIODO, segmento="parceira")
    sql = leitor.chamadas[0][0]
    assert "(CAB.CODTIPOPER IN (1116) OR (TPO.GOLSINAL = -1 AND CAB.CODEMP IN (1,25)))" in sql
    assert "NOT IN" not in sql and res["segmento"] == "parceira"


def test_segmento_fora_do_universo_soma(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["vendas_por_produto"].fn(ctx, **PERIODO, segmento="servicos")
    sql = leitor.chamadas[0][0]
    assert "(CAB.CODTIPOPER IN (1119,1125))" in sql and "ELSE 1 END" in sql


def test_perguntar_pela_empresa_do_segmento_seleciona_ele(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["vendas_resumo"].fn(ctx, **PERIODO, codemp=1)
    assert res["segmento"] == "parceira" and "NOT IN" not in leitor.chamadas[0][0]


def test_segmento_desconhecido_lista_validos(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="parceira, servicos"):
        tools["vendas_resumo"].fn(ctx, **PERIODO, segmento="xyz")


def test_segmento_vazio_e_recusado(tmp_path):
    ctx, tools, _ = _ctx(tmp_path, regra='[vendas.segmentos.vazio]\nnome = "x"\n')
    with pytest.raises(PoliticaViolada, match="vazio"):
        tools["vendas_resumo"].fn(ctx, **PERIODO)


def test_lista_segmentos(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    linhas = tools["segmentos_venda"].fn(ctx)["linhas"]
    assert linhas[1] == {"SEGMENTO": "servicos", "DESCRICAO": "Serviços", "TOPS": "1119,1125", "EMPRESAS": ""}


def test_regra_da_dalla_separa_vero_e_servicos():
    with open(RAIZ / "tenants" / "dalla.vendas.toml", "rb") as f:
        segs = tomllib.load(f)["vendas"]["segmentos"]
    assert segs["vero"]["tops"] == [1116] and set(segs["servicos"]["tops"]) == {1119, 1120, 1125}


def test_sem_regra_vira_mensagem(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    ctx.tenant.regras.clear()
    with pytest.raises(PoliticaViolada, match="vendas"):
        tools["vendas_resumo"].fn(ctx, **PERIODO)


def test_agrupar_invalido(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="Válidos"):
        tools["vendas_resumo"].fn(ctx, **PERIODO, agrupar="xyz")


def test_periodo_acima_do_limite(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match="366"):
        tools["vendas_resumo"].fn(ctx, data_inicio="2024-01-01", data_fim="2026-01-01")


@pytest.mark.parametrize("agrupar", ["empresa", "mes", "dia", "operacao", "vendedor"])
def test_agrupamentos_de_nota_sao_sql_valido(tmp_path, agrupar):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["vendas_resumo"].fn(ctx, **PERIODO, agrupar=agrupar)
    assert ("TOP 50" in leitor.chamadas[0][0]) == (agrupar not in ("mes", "dia"))


def test_serie_no_tempo_usa_limite_da_politica(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["vendas_resumo"].fn(ctx, **PERIODO, agrupar="dia", top=5)
    assert leitor.chamadas[0][2] == ctx.max_linhas


@pytest.mark.parametrize("agrupar", ["produto", "grupo", "vendedor", "empresa", "mes"])
def test_agrupamentos_de_item_sao_sql_valido(tmp_path, agrupar):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["vendas_por_produto"].fn(ctx, **PERIODO, agrupar=agrupar)
    assert "ITE.VLRTOT - ISNULL(ITE.VLRDESC, 0)" in leitor.chamadas[0][0]
    assert res["resumo"]["qtd_liquida"] == 7.0


def test_filtros_de_produto_na_ordem_dos_parametros(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["vendas_por_produto"].fn(ctx, **PERIODO, codprod=5418, codgrupo=101, codvend=9)
    sql, params, _ = leitor.chamadas[0]
    assert params[-3:] == [5418, 101, 9]
    assert sql.index("ITE.CODPROD = ?") < sql.index("PRO.CODGRUPOPROD = ?") < sql.index("ITE.CODVEND = ?")


# --- schema configurável --------------------------------------------------------
def test_schema_padrao_nao_altera_sql():
    sql = "SELECT 1 FROM sankhya.TGFCAB"
    assert aplicar_schema(sql, "sankhya") is sql


def test_schema_do_cliente_troca_so_tabelas():
    sql = "SELECT 'br.com.sankhya.evo' FROM sankhya.TGFCAB C JOIN SANKHYA.TGFITE I ON 1 = 1"
    assert aplicar_schema(sql, "dbo") == "SELECT 'br.com.sankhya.evo' FROM dbo.TGFCAB C JOIN dbo.TGFITE I ON 1 = 1"


def test_schema_invalido_e_recusado():
    with pytest.raises(ValueError, match="schema"):
        SqlConfig(dsn="x", schema="dbo; DROP TABLE X")


# --- marca e ano anterior -------------------------------------------------------
class LeitorComparacao(LeitorVendas):
    """Devolve linhas com as chaves dos agrupamentos; o ano anterior vale metade."""

    def consultar(self, sql, params=(), max_linhas=200):
        validar_somente_leitura(sql)
        self.chamadas.append((sql, list(params), max_linhas))
        anterior = list(params)[0].year == 2025
        f = 0.5 if anterior else 1
        mes = "2025-10" if anterior else "2026-10"
        linha = {"CODEMP": 3, "CODPROD": 10, "MARCA": "DECA", "MES": mes, "VLR_VENDA": 1000 * f,
                 "VLR_DEVOLUCAO": 0, "VLR_LIQUIDO": 1000 * f, "NOTAS_VENDA": 4, "NOTAS_DEVOLUCAO": 0,
                 "NOTAS": 4, "QTD_LIQUIDA": 8 * f}
        return [linha], False


def _ctx_comparacao(tmp_path):
    ctx, tools, _ = _ctx(tmp_path)
    leitor = LeitorComparacao()
    ctx.sql = leitor
    return ctx, tools, leitor


def test_filtro_e_agrupamento_por_marca(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["vendas_por_produto"].fn(ctx, **PERIODO, agrupar="marca", marca=" deca ")
    sql, params, _ = leitor.chamadas[0]
    assert "GROUP BY RTRIM(PRO.MARCA)" in sql and "PRO.MARCA LIKE ?" in sql
    assert params[-1] == "%deca%" and res["marca"] == " deca "


def test_ano_anterior_filtra_pelas_chaves_do_top(tmp_path):
    ctx, tools, leitor = _ctx_comparacao(tmp_path)
    res = tools["vendas_por_produto"].fn(ctx, **PERIODO, agrupar="produto", top=10,
                                         comparar_ano_anterior=True)
    sql_ant, params_ant, _ = leitor.chamadas[2]  # atual: detalhe e total; depois o ano anterior
    assert "TOP" not in sql_ant.split("FROM")[0] and "ITE.CODPROD IN (?)" in sql_ant
    assert params_ant[:2] == [date(2025, 10, 1), date(2025, 10, 31)] and params_ant[-1] == 10
    l = res["linhas"][0]
    assert l["VLR_LIQUIDO_ANO_ANTERIOR"] == 500.0 and l["QTD_LIQUIDA_ANO_ANTERIOR"] == 4.0
    assert l["VAR_PCT"] == 100.0
    ant = res["resumo"]["ano_anterior"]
    assert ant["vlr_liquido"] == 500.0 and ant["var_pct_liquido"] == 100.0
    assert ant["periodo"] == "2025-10-01 a 2025-10-31"


def test_ano_anterior_em_serie_casa_pelo_mes(tmp_path):
    ctx, tools, leitor = _ctx_comparacao(tmp_path)
    res = tools["vendas_resumo"].fn(ctx, **PERIODO, agrupar="mes", comparar_ano_anterior=True)
    assert leitor.chamadas[2][0].count(" IN (") == leitor.chamadas[0][0].count(" IN (")
    assert res["linhas"][0]["VLR_VENDA_ANO_ANTERIOR"] == 500.0


def test_ano_anterior_de_29_de_fevereiro(tmp_path):
    from tacio.packs.vendas import _um_ano_antes
    assert _um_ano_antes(date(2028, 2, 29)) == date(2027, 2, 28)


def test_sem_comparacao_nao_roda_consulta_extra(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["vendas_resumo"].fn(ctx, **PERIODO)
    assert len(leitor.chamadas) == 2 and "ano_anterior" not in res["resumo"]
