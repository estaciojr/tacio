"""Configuração do tenant: um arquivo TOML por cliente."""
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def ler_variavel(nome: str) -> str:
    """Variável de ambiente; no Windows, cai para a variável do usuário gravada no registro.

    Clientes como o Claude Desktop (Store) herdam o ambiente de quando o Windows iniciou e
    não enxergam uma variável criada depois, mesmo reiniciando o app.
    """
    valor = os.environ.get(nome, "")
    if valor or os.name != "nt":
        return valor
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as chave:
            return str(winreg.QueryValueEx(chave, nome)[0])
    except OSError:
        return ""


@dataclass
class SqlConfig:
    dsn: str
    senha_env: str = "TACIO_SQL_PWD"
    timeout_s: int = 20
    leitura_suja: bool = True
    schema: str = "sankhya"  # schema das tabelas do ERP neste cliente

    def __post_init__(self) -> None:
        # O schema é interpolado no SQL: só identificador simples passa daqui.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.schema):
            raise ValueError(f"[sql] schema '{self.schema}' inválido.")

    def connection_string(self) -> str:
        pwd = ler_variavel(self.senha_env)
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
class OAuthConfig:
    """Login OAuth no transporte HTTP (substitui o token fixo). Senha conferida no Sankhya."""
    url_publica: str                  # https://... (só http://localhost para teste local)
    sankhya_url: str                  # onde o login do usuário é conferido (MobileLoginSP)
    cnpjs: list[str]                  # CNPJs aceitos na tela de login
    banco: str = "dados/oauth.db"     # clientes, códigos e tokens (só hashes)
    usuarios_permitidos: list[str] = field(default_factory=list)  # vazio = qualquer usuário do Sankhya
    usuarios_sql_livre: list[str] = field(default_factory=list)   # quem pode usar sql_consulta
    ttl_acesso_s: int = 3600
    ttl_refresh_dias: int = 30


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
    oauth: OAuthConfig | None = None


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
        oauth=OAuthConfig(**raw["oauth"]) if "oauth" in raw else None,
    )
