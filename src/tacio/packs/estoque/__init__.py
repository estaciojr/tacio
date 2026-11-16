"""Pack estoque: ruptura, risco de ruptura e posição de estoque.

A regra de ruptura não está no código: os parâmetros vêm de ctx.regra("ruptura")
(ex.: tenants/dalla.ruptura.toml). Conceito:
- Universo: produto ativo "de linha" que vendeu na loja nos últimos `dias_giro` dias.
- Venda: TOPs com GOLSINAL = -1 (vendas e devoluções de venda), TGFTOP casada por
  DHTIPOPER; quantidade líquida = QTDNEG * GOLSINAL * GOLDEV * -1.
- Estoque disponível: SUM(ESTOQUE - RESERVADO) da TGFEST, estoque próprio (TIPO 'P'),
  só nos locais de venda configurados (fora: reserva de encomenda, almoxarifado, ATEC...).
- Venda sob encomenda (campo do item configurado) fica fora do giro e da venda média.
- Ruptura: disponível <= 0. Iminente/alerta: cobertura em dias até os limites configurados.
- Volumoso: com estoque no CD não é ruptura nem risco da loja (status CD_ATENDE).
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ...pack import Contexto, Pack, filtro_in
from ...policy import PoliticaViolada

pack = Pack("estoque", "Ruptura e posição de estoque")

_ESTOQUE_PROPRIO = "EST.TIPO = 'P'"
_IDENTIFICADOR = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_ORDEM_STATUS = {"RUPTURA": 0, "IMINENTE": 1, "ALERTA": 2, "CD_ATENDE": 3, "OK": 4}


@dataclass
class RegraRuptura:
    dias_giro: int
    iminente_dias: int
    alerta_dias: int
    lojas: list[int]
    empresa_cd: int
    campo_ativo: str
    valores_ativo: list[Any]
    grupos_volumoso: list[int]
    locais_loja: list[int]
    locais_cd: list[int]
    campo_encomenda: str
    valor_encomenda: Any

    @classmethod
    def de(cls, raw: dict) -> "RegraRuptura":
        r = raw.get("ruptura") or {}
        ativo = r.get("produto_ativo") or {}
        encomenda = r.get("encomenda") or {}
        regra = cls(
            dias_giro=int(r.get("dias_giro", 90)),
            iminente_dias=int(r.get("iminente_dias", 15)),
            alerta_dias=int(r.get("alerta_dias", 30)),
            # lojas, CD e grupos são interpolados no SQL: só inteiros passam daqui.
            lojas=[int(x) for x in r.get("lojas", [])],
            empresa_cd=int(r.get("empresa_cd", 0)),
            campo_ativo=str(ativo.get("campo", "")).upper(),
            valores_ativo=list(ativo.get("valores", [])),
            grupos_volumoso=[int(x) for x in (r.get("volumoso") or {}).get("grupos", [])],
            # Locais que contam como estoque à venda, separados para loja e CD (vazio = todos).
            locais_loja=[int(x) for x in r.get("locais_estoque_loja", [])],
            locais_cd=[int(x) for x in r.get("locais_estoque_cd", [])],
            campo_encomenda=str(encomenda.get("campo_item", "")).upper(),
            valor_encomenda=encomenda.get("valor", "S"),
        )
        if not regra.lojas:
            raise PoliticaViolada("Regra de ruptura sem lojas configuradas.")
        if not 1 <= regra.iminente_dias <= regra.alerta_dias:
            raise PoliticaViolada("Regra de ruptura: iminente_dias deve ser >= 1 e <= alerta_dias.")
        if regra.campo_ativo and not _IDENTIFICADOR.match(regra.campo_ativo):
            raise PoliticaViolada(f"Regra de ruptura: campo '{regra.campo_ativo}' inválido.")
        if regra.campo_encomenda and not _IDENTIFICADOR.match(regra.campo_encomenda):
            raise PoliticaViolada(f"Regra de ruptura: campo '{regra.campo_encomenda}' inválido.")
        if regra.campo_ativo and not regra.valores_ativo:
            raise PoliticaViolada("Regra de ruptura: produto_ativo.valores vazio.")
        return regra


def _ints(valores: list[int]) -> str:
    return ",".join(str(int(v)) for v in valores)


def _status(disp: float, disp_cd: float, cobertura: float | None, volumoso: bool,
            regra: RegraRuptura) -> str:
    if disp <= 0:
        status = "RUPTURA"
    elif cobertura is not None and cobertura <= regra.iminente_dias:
        status = "IMINENTE"
    elif cobertura is not None and cobertura <= regra.alerta_dias:
        status = "ALERTA"
    else:
        return "OK"
    # Volumoso sai do CD para o cliente: com estoque no CD, a loja não está rompida.
    return "CD_ATENDE" if volumoso and disp_cd > 0 else status


@pack.tool(descricao="""
Ruptura nas lojas: produtos de linha que a loja vende (giro nos últimos dias configurados)
e estão sem estoque disponível. Com incluir_risco=true traz também os IMINENTES (zeram em
poucos dias) e em ALERTA. Volumosos (pisos, porcelanatos, revestimentos) com estoque no CD
não contam como ruptura. Ordenado por status e pelo valor vendido no período (impacto).
Traz o índice de ruptura da rede/loja (por itens e por venda). Use para 'o que está
faltando nas lojas', 'índice de ruptura' ou 'o que vai faltar'.
- agrupar: item (padrão, lista de produtos) | loja (uma linha por loja com posições, rupturas,
  iminentes, alertas e os dois índices). Para comparar lojas, use loja numa chamada só em vez de
  chamar loja a loja.
""")
def ruptura_por_loja(ctx: Contexto, codemp: int | None = None, incluir_risco: bool = False,
                     top: int = 100, dias: int | None = None, agrupar: str = "item") -> dict:
    if agrupar not in ("item", "loja"):
        raise PoliticaViolada("agrupar inválido. Válidos: item, loja.")
    regra = RegraRuptura.de(ctx.regra("ruptura"))
    dias = regra.dias_giro if dias is None else int(dias)
    if not 7 <= dias <= 365:
        raise PoliticaViolada("dias deve estar entre 7 e 365.")
    top = max(1, min(int(top), ctx.max_linhas))
    empresas = ctx.politica.validar_codemp(codemp)
    if codemp is not None and codemp not in regra.lojas:
        raise PoliticaViolada(f"Empresa {codemp} não é loja analisada na ruptura "
                              f"(lojas: {_ints(regra.lojas)}).")
    f_cab, p_cab = filtro_in("CAB.CODEMP", empresas)
    desde = date.today() - timedelta(days=dias)

    filtro_ativo, p_ativo = "", []
    if regra.campo_ativo:
        filtro_ativo = f" AND PRO.{regra.campo_ativo} IN ({','.join('?' * len(regra.valores_ativo))})"
        p_ativo = regra.valores_ativo
    volumoso = (f"CASE WHEN PRO.CODGRUPOPROD IN ({_ints(regra.grupos_volumoso)}) THEN 1 ELSE 0 END"
                if regra.grupos_volumoso else "0")
    lojas, cd = _ints(regra.lojas), int(regra.empresa_cd)
    local_loja = f" AND EST.CODLOCAL IN ({_ints(regra.locais_loja)})" if regra.locais_loja else ""
    local_cd = f" AND EST.CODLOCAL IN ({_ints(regra.locais_cd)})" if regra.locais_cd else ""
    filtro_encomenda, p_encomenda = "", []
    if regra.campo_encomenda:
        filtro_encomenda = f" AND ISNULL(ITE.{regra.campo_encomenda}, '') <> ?"
        p_encomenda = [regra.valor_encomenda]
    qtd_liquida = "ITE.QTDNEG * TPO.GOLSINAL * TPO.GOLDEV * -1"
    sql = f"""
        WITH VENDAS AS (
            SELECT CAB.CODEMP, ITE.CODPROD,
                   SUM({qtd_liquida})                                  AS QTD,
                   SUM(ITE.VLRTOT * TPO.GOLSINAL * TPO.GOLDEV * -1)    AS VLR
            FROM sankhya.TGFCAB CAB
            JOIN sankhya.TGFTOP TPO ON TPO.CODTIPOPER = CAB.CODTIPOPER
                                   AND TPO.DHALTER = CAB.DHTIPOPER
            JOIN sankhya.TGFITE ITE ON ITE.NUNOTA = CAB.NUNOTA
            WHERE TPO.GOLSINAL = -1 AND CAB.STATUSNOTA = 'L'
              AND CAB.DTNEG >= ? AND CAB.CODEMP IN ({lojas}){f_cab}{filtro_encomenda}
            GROUP BY CAB.CODEMP, ITE.CODPROD
            HAVING SUM({qtd_liquida}) > 0
        ),
        ESTQ AS (
            SELECT EST.CODEMP, EST.CODPROD, SUM(EST.ESTOQUE - EST.RESERVADO) AS DISPONIVEL
            FROM sankhya.TGFEST EST
            WHERE {_ESTOQUE_PROPRIO}
              AND ((EST.CODEMP IN ({lojas}){local_loja}) OR (EST.CODEMP = {cd}{local_cd}))
            GROUP BY EST.CODEMP, EST.CODPROD
        )
        SELECT V.CODEMP, V.CODPROD, PRO.DESCRPROD, PRO.CODGRUPOPROD,
               {volumoso}                    AS VOLUMOSO,
               COALESCE(E.DISPONIVEL, 0)     AS DISPONIVEL,
               COALESCE(CD.DISPONIVEL, 0)    AS DISPONIVEL_CD,
               V.QTD                         AS QTD_VENDIDA_PERIODO,
               V.VLR                         AS VLR_VENDIDO_PERIODO
        FROM VENDAS V
        JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = V.CODPROD
        LEFT JOIN ESTQ E  ON E.CODEMP = V.CODEMP AND E.CODPROD = V.CODPROD
        LEFT JOIN ESTQ CD ON CD.CODEMP = {cd} AND CD.CODPROD = V.CODPROD
        WHERE PRO.ATIVO = 'S'{filtro_ativo}
    """
    # Busca o universo inteiro (posições ativas) para calcular o índice; devolve só o top.
    universo, _ = ctx.sql.consultar(sql, [desde, *p_cab, *p_encomenda, *p_ativo],
                                    max_linhas=200_000)

    contagem = dict.fromkeys(_ORDEM_STATUS, 0)
    vlr_total = vlr_ruptura = 0.0
    saida: list[dict] = []
    por_loja: dict[int, dict] = {}
    for l in universo:
        disp, disp_cd = float(l["DISPONIVEL"] or 0), float(l["DISPONIVEL_CD"] or 0)
        vd_dia = float(l["QTD_VENDIDA_PERIODO"] or 0) / dias
        cobertura = disp / vd_dia if vd_dia > 0 and disp > 0 else (0.0 if disp <= 0 else None)
        st = _status(disp, disp_cd, cobertura, bool(l["VOLUMOSO"]), regra)
        contagem[st] += 1
        vlr = float(l["VLR_VENDIDO_PERIODO"] or 0)
        vlr_total += vlr
        if st == "RUPTURA":
            vlr_ruptura += vlr
        loja = por_loja.setdefault(l["CODEMP"], {**dict.fromkeys(_ORDEM_STATUS, 0), "VLR": 0.0, "VLR_RUPTURA": 0.0})
        loja[st] += 1
        loja["VLR"] += vlr
        if st == "RUPTURA":
            loja["VLR_RUPTURA"] += vlr
        if st == "RUPTURA" or (incluir_risco and st in ("IMINENTE", "ALERTA")):
            saida.append({
                "CODEMP": l["CODEMP"], "CODPROD": l["CODPROD"], "DESCRPROD": l["DESCRPROD"],
                "STATUS": st, "DISPONIVEL": disp, "DISPONIVEL_CD": disp_cd,
                "VENDA_MEDIA_DIA": round(vd_dia, 2),
                "COBERTURA_DIAS": None if cobertura is None else round(cobertura, 1),
                "VLR_VENDIDO_PERIODO": round(vlr, 2), "VOLUMOSO": bool(l["VOLUMOSO"]),
            })
    saida.sort(key=lambda r: (_ORDEM_STATUS[r["STATUS"]], -r["VLR_VENDIDO_PERIODO"]))
    if agrupar == "loja":
        nomes = {e["CODEMP"]: e.get("EMPRESA") for e in ctx.sql.consultar(
            f"SELECT CODEMP, NOMEFANTASIA AS EMPRESA FROM sankhya.TSIEMP WHERE CODEMP IN ({lojas})", (), 500)[0]}
        saida = []
        for emp, c in por_loja.items():
            posicoes = sum(c[st] for st in _ORDEM_STATUS)
            saida.append({
                "CODEMP": emp, "EMPRESA": nomes.get(emp), "POSICOES_ATIVAS": posicoes, "RUPTURAS": c["RUPTURA"],
                "IMINENTES": c["IMINENTE"], "ALERTAS": c["ALERTA"], "VOLUMOSOS_CD_ATENDE": c["CD_ATENDE"],
                "INDICE_RUPTURA_ITENS": round(c["RUPTURA"] / posicoes * 100, 2) if posicoes else 0.0,
                "INDICE_RUPTURA_VENDA": round(c["VLR_RUPTURA"] / c["VLR"] * 100, 2) if c["VLR"] else 0.0,
                "VLR_VENDIDO_PERIODO": round(c["VLR"], 2),
            })
        saida.sort(key=lambda r: -r["INDICE_RUPTURA_ITENS"])
        top = max(top, len(saida))  # resumo por loja sempre vem inteiro
    ativos = len(universo)
    return ctx.politica.resultado(
        saida[:top], len(saida) > top,
        periodo_dias=dias,
        posicoes_ativas=ativos,
        rupturas=contagem["RUPTURA"], iminentes=contagem["IMINENTE"], alertas=contagem["ALERTA"],
        volumosos_cd_atende=contagem["CD_ATENDE"],
        indice_ruptura_itens=round(contagem["RUPTURA"] / ativos * 100, 2) if ativos else 0.0,
        indice_ruptura_venda=round(vlr_ruptura / vlr_total * 100, 2) if vlr_total else 0.0,
    )


@pack.tool(descricao="""
Posição de estoque de um produto por empresa: estoque, reservado e disponível.
Use para 'tem X em estoque?' ou 'em qual loja tem X'.
""")
def estoque_produto(ctx: Contexto, codprod: int, codemp: int | None = None) -> dict:
    f_est, p_est = filtro_in("EST.CODEMP", ctx.politica.validar_codemp(codemp))
    sql = f"""
        SELECT EST.CODEMP, PRO.CODPROD, PRO.DESCRPROD,
               SUM(EST.ESTOQUE) AS ESTOQUE,
               SUM(EST.RESERVADO) AS RESERVADO,
               SUM(EST.ESTOQUE - EST.RESERVADO) AS DISPONIVEL
        FROM sankhya.TGFEST EST
        JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = EST.CODPROD
        WHERE {_ESTOQUE_PROPRIO} AND EST.CODPROD = ?{f_est}
        GROUP BY EST.CODEMP, PRO.CODPROD, PRO.DESCRPROD
        ORDER BY EST.CODEMP
    """
    linhas, trunc = ctx.sql.consultar(sql, [codprod, *p_est], ctx.max_linhas)
    return ctx.politica.resultado(linhas, trunc, codprod=codprod)
