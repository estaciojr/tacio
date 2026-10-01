"""Análise opcional por LLM. Só roda em operators com analisar_com_llm = true.

Recebe dados já filtrados e mascarados pela política. Se a chamada falhar, o operator
envia o alerta sem análise; o LLM nunca bloqueia um alerta.
"""
import json
import os
import urllib.request
from typing import Any

_SISTEMA = """Você é o Tacio, analista de operações de uma rede de home centers que usa o ERP Sankhya.
Recebe o resultado de um monitor automático e escreve uma análise curta em português do Brasil
para a equipe responsável. Regras: no máximo 6 linhas; comece pelo que exige ação hoje;
agrupe por loja ou padrão quando fizer sentido; cite códigos de produto/empresa;
não invente dados que não estão no resultado; sem saudação nem despedida."""


def analisar(modelo: str, api_key_env: str, operador: str, instrucao: str,
             novos: list[dict[str, Any]], persistentes: int, resolvidos: int,
             timeout: int = 60) -> str:
    chave = os.environ.get(api_key_env)
    if not chave:
        raise RuntimeError(f"Variável {api_key_env} não definida.")
    dados = json.dumps(novos[:60], ensure_ascii=False, default=str)
    prompt = (
        f"Monitor: {operador}\n"
        f"{instrucao}\n\n"
        f"Novos alertas ({len(novos)}): {dados}\n"
        f"Alertas que continuam abertos: {persistentes}\n"
        f"Resolvidos desde a última execução: {resolvidos}"
    )
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({
            "model": modelo, "max_tokens": 600, "system": _SISTEMA,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8"),
        headers={"x-api-key": chave, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        corpo = json.loads(resp.read())
    return "".join(b.get("text", "") for b in corpo.get("content", []) if b.get("type") == "text").strip()
