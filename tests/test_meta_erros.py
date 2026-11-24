from dataclasses import replace

import pytest

from tacio.pack import carregar_tools, montar_contexto
from tacio.policy import PoliticaViolada

from test_nucleo import _tenant

COLUNA_RUIM = ("('42S22', \"[42S22] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
               "Invalid column name 'DESCRGRUPO'. (207) (SQLExecDirectW)\")")


class LeitorErro:
    def __init__(self, erro):
        self.erro, self.chamadas = erro, []

    def consultar(self, sql, params=(), max_linhas=200):
        self.chamadas.append(sql)
        if "TDDCAM" in sql:
            return [{"NOMETAB": "TGFGRU", "NOMECAMPO": "DESCRGRUPOPROD"},
                    {"NOMETAB": "TGFGRU", "NOMECAMPO": "CODGRUPOPROD"},
                    {"NOMETAB": "TGFGRU", "NOMECAMPO": "ATIVO"}], False
        raise Exception(self.erro)


def _ctx(tmp_path, erro):
    t = replace(_tenant(tmp_path, sql_livre=True, tabelas_sql_livre=["*"]), packs=["meta"])
    leitor = LeitorErro(erro)
    return montar_contexto(t, leitor), carregar_tools(t), leitor


def test_coluna_invalida_sugere_parecidas(tmp_path):
    ctx, tools, leitor = _ctx(tmp_path, COLUNA_RUIM)
    with pytest.raises(PoliticaViolada) as e:
        tools["sql_consulta"].fn(ctx, sql="SELECT DESCRGRUPO FROM sankhya.TGFGRU")
    msg = str(e.value)
    assert "'DESCRGRUPO'" in msg and "TGFGRU.DESCRGRUPOPROD" in msg and "descrever_tabela" in msg
    assert "TGFGRU.ATIVO" not in msg


@pytest.mark.parametrize("erro,trecho", [
    ("[SQL Server]The text, ntext, and image data types cannot be compared (402) (SQLExecDirectW)", r"AS varchar.max."),
    ("[SQL Server]Operand data type varchar is invalid for sum operator. (8117)", "CAST"),
    ("[HYT00] [Microsoft][ODBC Driver 18 for SQL Server]Query timeout expired (0)", "reduza o período"),
])
def test_dicas_tsql(tmp_path, erro, trecho):
    ctx, tools, _ = _ctx(tmp_path, erro)
    with pytest.raises(PoliticaViolada, match=trecho):
        tools["sql_consulta"].fn(ctx, sql="SELECT 1 FROM sankhya.TGFCAB")


def test_erro_desconhecido_segue_como_erro_tecnico(tmp_path):
    ctx, tools, _ = _ctx(tmp_path, "falha qualquer de rede")
    with pytest.raises(Exception, match="falha qualquer") as e:
        tools["sql_consulta"].fn(ctx, sql="SELECT 1 FROM sankhya.TGFCAB")
    assert not isinstance(e.value, PoliticaViolada)
