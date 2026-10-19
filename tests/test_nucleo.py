import asyncio
import json
from datetime import date
from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tacio.adapters.sql_read import validar_somente_leitura
from tacio.config import HttpConfig, PoliticaConfig, SqlConfig, TenantConfig
from tacio.policy import Politica, PoliticaViolada, corrigir_encoding, mascarar_cpf
from tacio.server import criar_servidor


# --- guarda SQL --------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "SELECT * FROM TGFCAB",
    "WITH X AS (SELECT NUNOTA FROM TGFCAB) SELECT * FROM X",
    "SELECT CODEMP FROM TGFCAB UNION SELECT CODEMP FROM TSIEMP",
])
def test_select_aceito(sql):
    validar_somente_leitura(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM TGFCAB",
    "UPDATE TGFPRO SET DESCRPROD = 'x'",
    "SELECT * INTO TMP FROM TGFCAB",
    "SELECT 1; DROP TABLE TGFCAB",
    "EXEC sp_who",
    "TRUNCATE TABLE TGFCAB",
])
def test_escrita_bloqueada(sql):
    with pytest.raises(PoliticaViolada):
        validar_somente_leitura(sql)


def test_whitelist_tabelas():
    validar_somente_leitura("SELECT * FROM TGFCAB C JOIN TGFITE I ON I.NUNOTA=C.NUNOTA",
                            ["TGFCAB", "TGFITE"])
    with pytest.raises(PoliticaViolada, match="TSIUSU"):
        validar_somente_leitura("SELECT * FROM TSIUSU", ["TGFCAB"])


# --- política ----------------------------------------------------------------
def test_mascara_cpf_preserva_cnpj():
    assert mascarar_cpf("cliente 123.456.789-09") == "cliente ***.456.***-**"
    assert mascarar_cpf("12345678909") == "***.456.***-**"
    assert mascarar_cpf("12.345.678/0001-90") == "12.345.678/0001-90"
    assert mascarar_cpf("12345678000190") == "12345678000190"


def test_corrige_double_encoding():
    assert corrigir_encoding("ConstruÃ§Ã£o") == "Construção"
    assert corrigir_encoding("Construção") == "Construção"


def test_codemp_fora_do_escopo():
    p = Politica(PoliticaConfig(), codemp_permitidas=[1, 2])
    assert p.validar_codemp(None) == [1, 2]
    assert p.validar_codemp(2) == [2]
    with pytest.raises(PoliticaViolada):
        p.validar_codemp(9)


def test_resultado_serializa_tipos_sql():
    p = Politica(PoliticaConfig(colunas_mascaradas=["CGC_CPF"]), [])
    r = p.resultado([{"VLR": Decimal("10.50"), "DT": date(2026, 9, 1), "CGC_CPF": "12345678909"}],
                    truncado=True)
    assert r["linhas"][0] == {"VLR": 10.5, "DT": "2026-09-01", "CGC_CPF": "***.456.***-**"}
    assert r["truncado"] and "aviso" in r
    json.dumps(r)


# --- servidor de ponta a ponta (sem banco) -----------------------------------
class LeitorFalso:
    def __init__(self):
        self.chamadas = []

    def consultar(self, sql, params=(), max_linhas=200):
        validar_somente_leitura(sql)
        self.chamadas.append((sql, list(params)))
        if "NOTAS_PARC_GRUPO" in sql:  # checagem de classificação do pack compras
            return [{"CODTIPOPER": 1410, "QTD_NOTAS": 1, "VLR_NOTAS": Decimal("1500"),
                     "NOTAS_PARC_GRUPO": 0}], False
        return [{"CODPARC": 10, "NOMEPARC": "FORNECEDOR ABC", "VLR_COMPRADO": Decimal("1500.00")}], False


TOPS_TESTE = """
[naturezas.revenda]
nome = "Compra de revenda"
compra = true
[naturezas.transferencia]
nome = "Transferência"
[naturezas.devolucao_compra]
nome = "Devolução de compra"
sinal = -1
[tops]
1410 = "revenda"
1456 = "transferencia"
1500 = "devolucao_compra"
[tops_pedido]
1310 = "revenda"
1305 = "transferencia"
"""


def _tenant(tmp_path, **pol):
    arq_tops = tmp_path / "tops.toml"
    arq_tops.write_text(TOPS_TESTE, encoding="utf-8")
    return TenantConfig(
        id="teste", nome="Teste", packs=["meta", "compras"], codemp_permitidas=[1],
        sql=SqlConfig(dsn="x"), politica=PoliticaConfig(**pol),
        auditoria_arquivo=str(tmp_path / "audit.jsonl"), http=HttpConfig(),
        classificacao_tops=str(arq_tops),
    )


def test_servidor_lista_tools_sem_ctx(tmp_path):
    srv = criar_servidor(_tenant(tmp_path), sql=LeitorFalso())
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    assert {"descrever_tabela", "buscar_campo", "sql_consulta",
            "ultimas_compras_produto", "compras_por_fornecedor"} <= set(tools)
    props = tools["compras_por_fornecedor"].input_schema["properties"]
    assert "ctx" not in props and {"data_inicio", "data_fim", "codemp", "top"} <= set(props)


def test_chamada_aplica_politica_e_audita(tmp_path):
    leitor = LeitorFalso()
    srv = criar_servidor(_tenant(tmp_path), sql=leitor)
    asyncio.run(srv.call_tool("compras_por_fornecedor",
                              {"data_inicio": "2026-09-01", "data_fim": "2026-09-30"}))
    sql, params = leitor.chamadas[-1]
    assert "CAB.CODEMP IN (?)" in sql and params[-1] == 1  # escopo do tenant aplicado
    log = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    evento = json.loads(log[-1])
    assert evento["tool"] == "compras_por_fornecedor" and evento["linhas"] == 1


def test_erro_de_politica_vira_mensagem(tmp_path):
    srv = criar_servidor(_tenant(tmp_path, sql_livre=False), sql=LeitorFalso())
    with pytest.raises(ToolError, match="desativada"):
        asyncio.run(srv.call_tool("sql_consulta", {"sql": "SELECT 1"}))
    log = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "desativada" in log


def test_erro_tecnico_nao_vaza_detalhe(tmp_path):
    class LeitorQuebrado:
        def consultar(self, *a, **k):
            raise RuntimeError("Login failed for user tacio_leitura")
    srv = criar_servidor(_tenant(tmp_path), sql=LeitorQuebrado())
    with pytest.raises(ToolError) as e:
        asyncio.run(srv.call_tool("buscar_campo", {"termo": "margem"}))
    assert "Login failed" not in str(e.value)
    assert "Login failed" in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
