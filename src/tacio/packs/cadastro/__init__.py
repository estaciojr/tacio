"""Pack cadastro: busca de parceiro (cliente, fornecedor, transportadora) e contato.

Conceito:
- Busca por trecho do nome fantasia ou da razão social, por CNPJ (só dígitos, inteiro ou a raiz)
  ou por lista de códigos (CODPARC).
- Contato (telefone, celular, e-mails) só sai de pessoa jurídica. Pessoa física volta só com nome,
  cidade e CPF mascarado: dado pessoal de cliente não vai para o chat (LGPD).
- Produto e grupo: busca por nome (todas as palavras), código, referência/EAN ou marca, para o agente
  achar o código antes de pedir venda, estoque ou compra.
- Campos de contato extras do cliente (ex.: AD_WHATSAPP) vêm de ctx.regra("cadastro"), se houver.
"""
import re

from ...pack import Contexto, Pack
from ...policy import PoliticaViolada

pack = Pack("cadastro", "Busca de parceiro, produto e grupo de produto")

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


_MAX_PALAVRAS = 6


@pack.tool(descricao="""
Procura produtos pelo nome, código, referência/EAN ou marca e devolve o código (CODPROD), o grupo e o
status de linha. Use ANTES de vendas_por_produto, estoque_produto ou compras quando o usuário citar um
produto só pelo nome ('colorante Suvinil', 'cuba Deca'), em vez de montar SQL.
- termo: palavras do nome (todas precisam aparecer, qualquer ordem), ou um número: código do produto
  ou referência/EAN exata.
- marca: trecho da marca (ex.: 'SUVINIL'). codgrupo: código exato do grupo (veja grupos_produto).
- status: código de AD_STATUS (1 De Linha, 2 Fora de Linha, 3 Encomenda, 4 Fora do Mix, 5 Sazonalidade,
  6 Indisponível para Venda).
- incluir_inativos=true traz também os inativos (padrão: só ativos).
Informe ao menos termo, marca ou codgrupo.
""")
def produto(ctx: Contexto, termo: str | None = None, marca: str | None = None,
            codgrupo: int | None = None, status: int | None = None,
            incluir_inativos: bool = False, top: int = 50) -> dict:
    where, params = [], []
    if termo and termo.strip():
        t = termo.strip()
        if t.isdigit():
            where.append("(PRO.CODPROD = ? OR PRO.REFERENCIA = ?)")
            params += [int(t) if len(t) <= 9 else -1, t]
        else:
            palavras = t.split()[:_MAX_PALAVRAS]
            if all(len(p) < 2 for p in palavras):
                raise PoliticaViolada("termo precisa de pelo menos 2 letras.")
            for pal in palavras:
                where.append("PRO.DESCRPROD LIKE ?")
                params.append(f"%{pal}%")
    if marca and marca.strip():
        where.append("PRO.MARCA LIKE ?")
        params.append(f"%{marca.strip()}%")
    if codgrupo is not None:
        where.append("PRO.CODGRUPOPROD = ?")
        params.append(int(codgrupo))
    if not where:
        raise PoliticaViolada("Informe termo, marca ou codgrupo.")
    if status is not None:
        where.append("PRO.AD_CODIGO = ?")
        params.append(int(status))
    if not incluir_inativos:
        where.append("PRO.ATIVO = 'S'")
    top = max(1, min(int(top), ctx.max_linhas))
    sql = f"""
        SELECT TOP {top} PRO.CODPROD, RTRIM(PRO.DESCRPROD) AS DESCRPROD, RTRIM(PRO.MARCA) AS MARCA,
               PRO.REFERENCIA, PRO.CODVOL, PRO.ATIVO, PRO.CODGRUPOPROD,
               RTRIM(GRU.DESCRGRUPOPROD) AS GRUPO, RTRIM(STA.DESCSTATUS) AS STATUS_LINHA
        FROM sankhya.TGFPRO PRO
        LEFT JOIN sankhya.TGFGRU GRU ON GRU.CODGRUPOPROD = PRO.CODGRUPOPROD
        LEFT JOIN sankhya.AD_STATUS STA ON STA.CODIGO = PRO.AD_CODIGO
        WHERE {' AND '.join(where)}
        ORDER BY PRO.ATIVO DESC, PRO.DESCRPROD"""
    linhas, trunc = ctx.sql.consultar(sql, params, top)
    return ctx.politica.resultado(linhas, trunc, termo=termo, marca=marca, codgrupo=codgrupo)


@pack.tool(descricao="""
Procura grupos de produto pelo nome ou lista os filhos de um grupo. Use para descobrir o código do grupo
('qual o grupo de tintas', 'grupos de iluminação') antes de filtrar vendas_por_produto ou produto por
codgrupo, em vez de montar SQL na TGFGRU.
- termo: trecho do nome do grupo (mínimo 2 letras).
- codgrupai: código do grupo pai, para listar os subgrupos dele.
- apenas_analiticos=true traz só os grupos que têm produto (os que servem de filtro de venda).
Devolve código, nome, grau, se é analítico (tem produto) e quantos produtos ativos tem.
""")
def grupos_produto(ctx: Contexto, termo: str | None = None, codgrupai: int | None = None,
                   apenas_analiticos: bool = False, top: int = 50) -> dict:
    where, params = ["GRU.ATIVO = 'S'"], []
    if termo and termo.strip():
        if len(termo.strip()) < 2:
            raise PoliticaViolada("termo precisa de pelo menos 2 letras.")
        where.append("GRU.DESCRGRUPOPROD LIKE ?")
        params.append(f"%{termo.strip()}%")
    elif codgrupai is None:
        raise PoliticaViolada("Informe termo ou codgrupai.")
    if codgrupai is not None:
        where.append("GRU.CODGRUPAI = ?")
        params.append(int(codgrupai))
    if apenas_analiticos:
        where.append("GRU.ANALITICO = 'S'")
    top = max(1, min(int(top), ctx.max_linhas))
    sql = f"""
        SELECT TOP {top} GRU.CODGRUPOPROD, RTRIM(GRU.DESCRGRUPOPROD) AS GRUPO, GRU.GRAU, GRU.ANALITICO,
               GRU.CODGRUPAI,
               CASE WHEN GRU.ANALITICO = 'S'
                    THEN (SELECT COUNT(*) FROM sankhya.TGFPRO PRO
                          WHERE PRO.CODGRUPOPROD = GRU.CODGRUPOPROD AND PRO.ATIVO = 'S') END AS PRODUTOS_ATIVOS
        FROM sankhya.TGFGRU GRU
        WHERE {' AND '.join(where)}
        ORDER BY GRU.CODGRUPOPROD"""
    linhas, trunc = ctx.sql.consultar(sql, params, top)
    return ctx.politica.resultado(linhas, trunc, termo=termo, codgrupai=codgrupai)
