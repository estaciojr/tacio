# Tacio Operators

Monitores agendados: chamam uma tool do Tacio, filtram o resultado, comparam com a
execução anterior e avisam só o que é novo ou foi resolvido.

## Como funciona cada execução

1. Roda a tool do manifesto (mesma política e auditoria do servidor MCP).
2. Aplica `[operator.filtro]` (igualdade/lista) e `[operator.minimo]` (valor mínimo).
3. Compara com o estado salvo usando as colunas de `chave`:
   novos, persistentes (não repete) e resolvidos.
4. Opcional: `analisar_com_llm = true` adiciona um resumo priorizado no topo.
   Se o LLM falhar, o alerta sai do mesmo jeito.
5. Envia para os canais. Se nenhum canal aceitar, o estado não é gravado:
   na próxima execução o alerta é reenviado.

## Comandos

```powershell
tacio-operators --tenant tenants\dalla.toml listar
tacio-operators --tenant tenants\dalla.toml executar ruptura_diaria --simular
tacio-operators --tenant tenants\dalla.toml executar ruptura_diaria
tacio-operators --tenant tenants\dalla.toml rodar
```

`--simular` mostra a mensagem sem enviar e sem gravar estado. Use sempre antes de ligar
um operator novo. O `rodar` recarrega os manifestos sozinho quando um TOML muda.

## Manifesto

```toml
[operator]
id = "ruptura_diaria"                 # único
nome = "Ruptura nas lojas"            # aparece no título da mensagem
agenda = "0 7 * * 1-6"                # cron: min hora dia mês dia_semana (0/7 = domingo)
tool = "ruptura_por_loja"             # qualquer tool dos packs ativos
params = { dias = 30, incluir_risco = false }
canais = ["compras_telegram"]         # nomes definidos em [canais.*] do tenant
chave = ["CODEMP", "CODPROD"]         # o que identifica "o mesmo alerta"
campos_msg = ["CODEMP", "CODPROD", "DESCRPROD"]
max_itens_msg = 20
avisar_resolvidos = true
enviar_se_vazio = false               # true = manda "tudo ok" mesmo sem novidade
analisar_com_llm = false
instrucao_llm = "Priorize por valor vendido."
ativo = true

[operator.filtro]
STATUS = ["RUPTURA"]

[operator.minimo]
VLR_VENDIDO_PERIODO = 300
```

## Canais

Configurados no TOML do tenant (`[canais.<nome>]`), segredos sempre em variável de ambiente:

- `console`: imprime no terminal (testes).
- `telegram`: bot do Telegram (`token_env`, `chat_id`).
- `teams`: webhook de Workflow do Teams ("Post to a channel when a webhook request is
  received"), enviado como Adaptive Card (`url_env`).
- `email`: SMTP com STARTTLS (`smtp_host`, `usuario`, `senha_env`, `para`).

## LLM e LGPD

Só operators com `analisar_com_llm = true` enviam dados para a API da Anthropic, e só os
alertas novos (até 60), já com CPF mascarado pela política. Variável: `ANTHROPIC_API_KEY`.

## Rodar como serviço no Windows

Use o NSSM (ou o Agendador de Tarefas com gatilho "Ao iniciar"):

```powershell
nssm install TacioOperators "C:\caminho\.venv\Scripts\tacio-operators.exe" "--tenant C:\caminho\tenants\dalla.toml rodar"
nssm set TacioOperators AppDirectory C:\caminho
nssm set TacioOperators AppEnvironmentExtra TACIO_SQL_PWD=... TACIO_TELEGRAM_TOKEN=...
nssm start TacioOperators
```

O `AppDirectory` importa: `logs/` e `dados/` são relativos a ele.

## Histórico

`dados/operators.db` (SQLite) guarda o estado dos alertas e cada execução
(tabela `execucoes`: duração, linhas, novos, resolvidos, enviado, erro).
