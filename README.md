# Tacio

MCP Server para o Sankhya ERP: núcleo reutilizável + packs por domínio.
Um processo atende um tenant (cliente); vários clientes = um container por cliente.

    src/tacio/
      server.py             # único arquivo que conhece o SDK MCP; stdio ou HTTP com token
      config.py             # tenant em TOML
      policy.py             # limites, máscara de CPF, escopo de CODEMP, encoding
      audit.py              # auditoria JSONL de toda chamada
      pack.py               # contrato Pack + Contexto injetado
      adapters/sql_read.py  # SQL Server somente leitura, timeout, guarda sqlglot
      packs/meta/           # dicionário de dados (TDDCAM) + SQL livre guardado
      packs/compras/        # compras e fornecedores (padrão de referência)
      packs/estoque/        # ruptura, risco de ruptura, posição de estoque
      operators/            # Tacio Operators: agendador, estado, canais, LLM
    operators/              # manifestos TOML (um por monitor)

## Instalar e testar

    pip install -e .[dev]
    pytest -q

## Rodar

Copie `tenants/dalla.toml.example` para `tenants/dalla.toml` e ajuste o DSN.

Claude Desktop / Claude Code (stdio):

```json
{
  "mcpServers": {
    "tacio": {
      "command": "tacio",
      "args": ["--tenant", "C:/caminho/tenants/dalla.toml"],
      "env": { "TACIO_SQL_PWD": "..." }
    }
  }
}
```

Rede interna / outros agentes (HTTP, endpoint `/mcp`, porta do TOML):

```powershell
$env:TACIO_SQL_PWD="..."; $env:TACIO_TOKEN="<token longo>"
tacio --tenant tenants\dalla.toml --transport http
```

Clientes enviam `Authorization: Bearer <token>`.

Banco: crie o login `tacio_leitura` só com `db_datareader`, de preferência na réplica.

Veja `CRIAR_TOOLS.md` para adicionar tools e packs e `OPERATORS.md` para os monitores agendados.
