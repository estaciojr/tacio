from dataclasses import replace

import pytest

from tacio.adapters.sql_read import validar_somente_leitura
from tacio.pack import carregar_tools, montar_contexto
from tacio.policy import PoliticaViolada

from test_nucleo import _tenant


class LeitorCadastro:
    def __init__(self):
        self.chamadas = []

    def consultar(self, sql, params=(), max_linhas=200):
        validar_somente_leitura(sql)
        self.chamadas.append((sql, list(params), max_linhas))
        return [{"CODPARC": 1, "NOMEPARC": "FULANO", "TIPPESSOA": "F", "CGC_CPF": "12345678901",
                 "TELEFONE": None}], False


def _ctx(tmp_path, regra=None):
    regras = {}
    if regra is not None:
        arq = tmp_path / "cadastro.toml"
        arq.write_text(regra, encoding="utf-8")
        regras["cadastro"] = str(arq)
    t = replace(_tenant(tmp_path, colunas_mascaradas=["CGC_CPF"]), packs=["cadastro"], regras=regras)
    leitor = LeitorCadastro()
    return montar_contexto(t, leitor), carregar_tools(t), leitor


def test_busca_por_nome_so_ativos_e_contato_so_de_pj(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    res = tools["parceiro"].fn(ctx, termo=" gegel ")
    sql, params, _ = leitor.chamadas[0]
    assert "(PAR.NOMEPARC LIKE ? OR PAR.RAZAOSOCIAL LIKE ?)" in sql and params == ["%gegel%", "%gegel%"]
    assert "PAR.ATIVO = 'S'" in sql and "TOP 50" in sql
    assert "CASE WHEN PAR.TIPPESSOA = 'J' THEN PAR.TELEFONE END AS TELEFONE" in sql
    assert "CASE WHEN PAR.TIPPESSOA = 'J' THEN PAR.EMAIL END AS EMAIL" in sql
    assert "AD_WHATSAPP" not in sql
    assert res["linhas"][0]["CGC_CPF"] == "***.456.***-**"


def test_celular_de_pj_nao_vira_cpf_mascarado(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path, '[cadastro]\ncampos_contato = ["AD_WHATSAPP"]\n')
    leitor.consultar = lambda sql, params=(), max_linhas=200: ([{
        "CODPARC": 2, "TIPPESSOA": "J", "CGC_CPF": "04361389000151", "TELEFONE": "27999501234",
        "AD_WHATSAPP": "27999501234", "NOMEPARC": "POSTO 123.456.789-01"}], False)
    l = tools["parceiro"].fn(ctx, termo="posto")["linhas"][0]
    assert l["TELEFONE"] == l["AD_WHATSAPP"] == "27999501234"
    assert l["NOMEPARC"] == "POSTO ***.456.***-**"   # texto livre segue mascarado


def test_busca_por_cnpj_so_pessoa_juridica(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["parceiro"].fn(ctx, termo="12.345.678/0001-90")
    sql, params, _ = leitor.chamadas[0]
    assert "PAR.TIPPESSOA = 'J' AND PAR.CGC_CPF LIKE ?" in sql and params == ["12345678000190%"]


def test_lista_de_codigos_tipo_e_inativos(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path)
    tools["parceiro"].fn(ctx, codparcs=[4136, 11571, 4136], tipo="fornecedor", incluir_inativos=True)
    sql, params, _ = leitor.chamadas[0]
    assert "PAR.CODPARC IN (?,?)" in sql and params == [4136, 11571]
    assert "PAR.FORNECEDOR = 'S'" in sql and "PAR.ATIVO = 'S'" not in sql


def test_campo_de_contato_extra_da_regra(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path, '[cadastro]\ncampos_contato = ["ad_whatsapp"]\n')
    tools["parceiro"].fn(ctx, termo="wise")
    assert "CASE WHEN PAR.TIPPESSOA = 'J' THEN PAR.AD_WHATSAPP END AS AD_WHATSAPP" in leitor.chamadas[0][0]


@pytest.mark.parametrize("kwargs, msg", [
    ({}, "termo ou codparcs"), ({"termo": "ab"}, "3 letras"), ({"termo": "wise", "tipo": "x"}, "tipo inválido"),
    ({"codparcs": list(range(101))}, "100"),
])
def test_validacoes(tmp_path, kwargs, msg):
    ctx, tools, _ = _ctx(tmp_path)
    with pytest.raises(PoliticaViolada, match=msg):
        tools["parceiro"].fn(ctx, **kwargs)


def test_campo_extra_invalido_e_recusado(tmp_path):
    ctx, tools, _ = _ctx(tmp_path, '[cadastro]\ncampos_contato = ["X; DROP"]\n')
    with pytest.raises(PoliticaViolada, match="inválido"):
        tools["parceiro"].fn(ctx, termo="wise")
