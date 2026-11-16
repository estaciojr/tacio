"""Pack cadastro: busca de parceiro (cliente, fornecedor, transportadora) e contato.

Conceito:
- Busca por trecho do nome fantasia ou da razão social, por CNPJ (só dígitos, inteiro ou a raiz)
  ou por lista de códigos (CODPARC).
- Contato (telefone, celular, e-mails) só sai de pessoa jurídica. Pessoa física volta só com nome,
  cidade e CPF mascarado: dado pessoal de cliente não vai para o chat (LGPD).
- Campos de contato extras do cliente (ex.: AD_WHATSAPP) vêm de ctx.regra("cadastro"), se houver.
"""
import re

from ...pack import Contexto, Pack
from ...policy import PoliticaViolada

pack = Pack("cadastro", "Busca de parceiro e contato")

_IDENTIFICADOR = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_CONTATO = ["TELEFONE", "FAX", "EMAIL", "EMAILNFE"]
_MAX_CODIGOS = 100
_TIPOS = {"cliente": "CLIENTE", "fornecedor": "FORNECEDOR", "transportadora": "TRANSPORTADORA"}


def _contato_extra(ctx: Contexto) -> list[str]:
    if "cadastro" not in ctx.tenant.regras:
        return []
    campos = [str(c).upper() for c in ctx.regra("cadastro").get("cadastro", {}).get("campos_contato", [])]
    # Nome de coluna é interpolado no SQL: só identificador simples passa daqui.
    for c in campos:
        if not _IDENTIFICADOR.match(c):
            raise PoliticaViolada(f"Regra de cadastro: campo '{c}' inválido.")
    return campos


@pack.tool(descricao="""
Procura parceiros (clientes, fornecedores, transportadoras) e traz cadastro e contato. Use para
'qual o código do fornecedor X', 'CNPJ da empresa Y', 'telefone/e-mail do fornecedor Z' ou para
completar uma lista de códigos de parceiro que veio de outra tool.
- termo: trecho do nome ou da razão social (mínimo 3 letras), ou CNPJ só com dígitos (inteiro ou
  os 8 da raiz).
- codparcs: lista de códigos de parceiro (até 100), em vez de termo.
- tipo: cliente | fornecedor | transportadora (opcional).
- incluir_inativos=true traz também os inativos.
Contato só de pessoa jurídica; pessoa física vem sem telefone e e-mail, e o CPF mascarado.
""")
def parceiro(ctx: Contexto, termo: str | None = None, codparcs: list[int] | None = None,
             tipo: str | None = None, incluir_inativos: bool = False, top: int = 50) -> dict:
    where, params = [], []
    if codparcs:
        codigos = sorted({int(c) for c in codparcs})
        if len(codigos) > _MAX_CODIGOS:
            raise PoliticaViolada(f"No máximo {_MAX_CODIGOS} códigos por chamada.")
        where.append(f"PAR.CODPARC IN ({','.join('?' * len(codigos))})")
        params += codigos
    elif termo and termo.strip():
        t = termo.strip()
        digitos = re.sub(r"\D", "", t)
        if len(digitos) >= 8 and len(digitos) == len(re.sub(r"[\s./-]", "", t)):
            # CNPJ (ou raiz). CPF não é chave de busca: identificaria pessoa física.
            where.append("PAR.TIPPESSOA = 'J' AND PAR.CGC_CPF LIKE ?")
            params.append(f"{digitos}%")
        elif len(t) >= 3:
            where.append("(PAR.NOMEPARC LIKE ? OR PAR.RAZAOSOCIAL LIKE ?)")
            params += [f"%{t}%", f"%{t}%"]
        else:
            raise PoliticaViolada("termo precisa de pelo menos 3 letras (ou um CNPJ só com dígitos).")
    else:
        raise PoliticaViolada("Informe termo ou codparcs.")
    if tipo is not None:
        if tipo not in _TIPOS:
            raise PoliticaViolada(f"tipo inválido. Válidos: {', '.join(_TIPOS)}.")
        where.append(f"PAR.{_TIPOS[tipo]} = 'S'")
    if not incluir_inativos:
        where.append("PAR.ATIVO = 'S'")

    contato = _CONTATO + _contato_extra(ctx)
    colunas_contato = ", ".join(f"CASE WHEN PAR.TIPPESSOA = 'J' THEN PAR.{c} END AS {c}" for c in contato)
    top = max(1, min(int(top), ctx.max_linhas))
    sql = f"""
        SELECT TOP {top} PAR.CODPARC, PAR.NOMEPARC, PAR.RAZAOSOCIAL, PAR.TIPPESSOA, PAR.CGC_CPF,
               PAR.CLIENTE, PAR.FORNECEDOR, PAR.TRANSPORTADORA, PAR.ATIVO,
               CID.NOMECID AS CIDADE, UFS.UF, {colunas_contato}
        FROM sankhya.TGFPAR PAR
        LEFT JOIN sankhya.TSICID CID ON CID.CODCID = PAR.CODCID
        LEFT JOIN sankhya.TSIUFS UFS ON UFS.CODUF = CID.UF
        WHERE {' AND '.join(where)}
        ORDER BY PAR.ATIVO DESC, PAR.NOMEPARC"""
    linhas, trunc = ctx.sql.consultar(sql, params, top)
    # Contato só vem preenchido para PJ (CASE acima): celular com DDD não é CPF, não mascarar.
    return ctx.politica.resultado(linhas, trunc, sem_mascara_texto=frozenset(contato), termo=termo, tipo=tipo)
