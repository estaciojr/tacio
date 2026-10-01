"""Configuração do tenant: um arquivo TOML por cliente."""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SqlConfig:
    dsn: str
    senha_env: str = "TACIO_SQL_PWD"
    timeout_s: int = 20
    leitura_suja: bool = True

    def connection_string(self) -> str:
        pwd = os.environ.get(self.senha_env, "")
        return f"{self.dsn};PWD={pwd}" if pwd else self.dsn


@dataclass
class PoliticaConfig:
    max_linhas: int = 200
    colunas_mascaradas: list[str] = field(default_factory=list)
    mascarar_cpf_em_texto: bool = True
    sql_livre: bool = False
    tabelas_sql_livre: list[str] = field(default_factory=list)


@dataclass
class HttpConfig:
    host: str = "127.0.0.1"
    porta: int = 8765
    token_env: str = "TACIO_TOKEN"


@dataclass
class OperatorsConfig:
    diretorio: str = "operators"
    estado: str = "dados/operators.db"
    llm_modelo: str = "claude-haiku-4-5-20251001"
    llm_api_key_env: str = "ANTHROPIC_API_KEY"


@dataclass
class TenantConfig:
    id: str
    nome: str
    packs: list[str]
    codemp_permitidas: list[int]
    sql: SqlConfig
    politica: PoliticaConfig
    auditoria_arquivo: str
    http: HttpConfig
    operators: OperatorsConfig = field(default_factory=OperatorsConfig)
    canais: dict[str, dict] = field(default_factory=dict)
    # TOML com a classificação das TOPs por natureza (relativo ao TOML do tenant).
    classificacao_tops: str | None = None
    # Parâmetros de regra por domínio: nome -> TOML (ex.: {"ruptura": ".../dalla.ruptura.toml"}).
    regras: dict[str, str] = field(default_factory=dict)


def carregar(caminho: str | Path) -> TenantConfig:
    with open(caminho, "rb") as f:
        raw = tomllib.load(f)
    t = raw["tenant"]
    tops = raw.get("classificacao", {}).get("tops")
    if tops:
        tops = str((Path(caminho).parent / tops).resolve())
    regras = {nome: str((Path(caminho).parent / arq).resolve())
              for nome, arq in raw.get("regras", {}).items()}
    return TenantConfig(
        id=t["id"],
        nome=t.get("nome", t["id"]),
        packs=t.get("packs", ["meta"]),
        codemp_permitidas=t.get("codemp_permitidas", []),
        sql=SqlConfig(**raw["sql"]),
        politica=PoliticaConfig(**raw.get("politica", {})),
        auditoria_arquivo=raw.get("auditoria", {}).get("arquivo", "logs/auditoria.jsonl"),
        http=HttpConfig(**raw.get("http", {})),
        operators=OperatorsConfig(**raw.get("operators", {})),
        canais=raw.get("canais", {}),
        classificacao_tops=tops,
        regras=regras,
    )
