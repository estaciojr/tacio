# Criando tools e packs no Tacio

## Novo pack

1. Crie `src/tacio/packs/<nome>/__init__.py` com `pack = Pack("<nome>", "descrição")`.
2. Ative no TOML do tenant: `packs = ["meta", "compras", "<nome>"]`.

## Nova tool

```python
from ...pack import Contexto, Pack, filtro_in
from ...policy import PoliticaViolada, parse_data

@pack.tool(descricao="""
Diga QUANDO o agente deve usar esta tool, não só o que ela faz.
""")
def minha_tool(ctx: Contexto, data_inicio: str, codemp: int | None = None) -> dict:
    ini = parse_data(data_inicio, "data_inicio")
    filtro_emp, p_emp = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    sql = f"SELECT ... WHERE CAB.DTNEG >= ?{filtro_emp}"
    linhas, trunc = ctx.sql.consultar(sql, [ini, *p_emp], ctx.max_linhas)
    return ctx.politica.resultado(linhas, trunc)
```

## Regras

1. `ctx: Contexto` é sempre o 1º parâmetro; o agente não o vê.
2. Demais parâmetros com tipo explícito: viram o schema da tool.
3. SQL 100% parametrizado (`?`). Só números já validados (`TOP {n}`, dias) podem ser interpolados.
4. Datas entram como `str` AAAA-MM-DD e passam por `parse_data()`.
5. Consulta por empresa sempre passa por `validar_codemp()`.
6. Retorno sempre via `ctx.politica.resultado(...)`; prefira agregados a linhas soltas.
7. Erro de regra: `raise PoliticaViolada("mensagem para o agente")`. Erros técnicos
   o núcleo já trata (ficam na auditoria, o agente recebe mensagem genérica).
8. Um teste por tool com leitor falso, conferindo SQL e parâmetros
   (modelo em `tests/test_nucleo.py`).
9. Antes de produção, confira o resultado contra o dashboard/relatório equivalente.

## SDK MCP 2.x

O Tacio usa `mcp>=2.2` (classe `MCPServer`; o antigo `FastMCP` é do 1.x). No 2.x só
`ToolError` leva a mensagem ao agente; o núcleo converte `PoliticaViolada` nele.
Os packs não importam nada do SDK.
