"""Pack entregas: o que falta entregar, o que já foi entregue e a situação de uma nota.

Conceito (parâmetros em ctx.regra("entregas"), ex.: tenants/dalla.entregas.toml):
- Universo: item marcado para entrega (AD_ENTREGA = 'E') de nota de remessa confirmada. Retirada
  pelo cliente não tem baixa no sistema e fica de fora.
- Entregue: item com ocorrência de entrega na TGFTEL; a data real é a da primeira delas.
- Falta entregar: sem essa ocorrência e com saldo (QTDNEG - QTDCONFERIDA - QTDENTREGUE). PENDENTE
  da remessa não baixa com a entrega; é a devolução que consome o saldo.
- Previsão: AD_DTENTREGA é a data atual do item e muda no reagendamento.
- Sem baixa: passou de dias_sem_baixa da previsão sem nenhuma ocorrência; provável entrega não
  registrada, por isso fica fora de atrasada. Item com SAM continua pendente e é contado à parte.
- SAM: cada item da nota de reposição é um SAM, ligado pela TGFVAR à remessa de origem, à nova
  remessa e à entrada do avariado. A data do SAM é a da ocorrência; DTNEG vem da nota duplicada.
- As consultas partem das notas dos últimos janela_dias: a TGFTEL não tem índice em DHCHAMADA, e
  remessa antiga sem baixa acumula desde o início do uso.
Depende de campos personalizados (AD_ENTREGA, AD_DTENTREGA, TGFTEL.AD_*, AD_OCOENT).
"""
from dataclasses import dataclass

from ...pack import Contexto, Pack, filtro_in
from ...policy import PoliticaViolada, validar_periodo

pack = Pack("entregas", "Entregas pendentes, realizadas, SAM e situação de uma nota")

_DE = "FROM sankhya.TGFCAB CAB JOIN sankhya.TGFITE ITE ON ITE.NUNOTA = CAB.NUNOTA"
_SALDO = "(ITE.QTDNEG - ISNULL(ITE.QTDCONFERIDA, 0) - ISNULL(ITE.QTDENTREGUE, 0))"
_VLR_UNIT = "(CASE WHEN ITE.QTDNEG <> 0 THEN (ITE.VLRTOT - ISNULL(ITE.VLRDESC, 0)) / ITE.QTDNEG ELSE 0 END)"
_HOJE = "CAST(GETDATE() AS DATE)"
_SITUACOES = ("atrasada", "hoje", "futura", "sem_baixa", "sem_data")
_JOIN_MOTORISTA = """
    LEFT JOIN sankhya.TGFEMP EMO ON EMO.CODEMP = {a}.CODEMP
    LEFT JOIN sankhya.TGFORD ORD ON ORD.ORDEMCARGA = {a}.ORDEMCARGA AND ORD.CODEMP = ISNULL(EMO.CODEMPOC, EMO.CODEMP)
    LEFT JOIN sankhya.TGFPAR MOT ON MOT.CODPARC = ORD.CODPARCMOTORISTA"""
_ULTIMA_OCORRENCIA = """
    OUTER APPLY (SELECT TOP 1 T.AD_CODOCOR, T.DHCHAMADA FROM sankhya.TGFTEL T
                  WHERE T.AD_NUNOTA = {a}.NUNOTA AND T.AD_SEQUENCIA = {a}.SEQUENCIA AND T.AD_CODOCOR IS NOT NULL
                  ORDER BY T.DHCHAMADA DESC, T.NUREL DESC) ULT
    LEFT JOIN sankhya.AD_OCOENT OCO ON OCO.CODOCOR = ULT.AD_CODOCOR"""

_MEDIDAS_PENDENTE = """
    COUNT(DISTINCT P.NUNOTA)                                 AS NOTAS,
    COUNT(*)                                                 AS ITENS,
    SUM(P.SALDO)                                             AS QTD_PENDENTE,
    SUM(P.VLR)                                               AS VLR_PENDENTE,
    SUM(CASE WHEN ISNULL(P.ORDEMCARGA, 0) = 0 THEN 1 ELSE 0 END) AS ITENS_SEM_CARGA,
    SUM(P.COM_SAM)                                           AS ITENS_COM_SAM"""

# Modelos com {top} são ranking (corte é esperado); sem {top} são série no tempo (corte é aviso).
_POR_SITUACAO = "SELECT P.SITUACAO, {medidas} FROM P GROUP BY P.SITUACAO ORDER BY P.SITUACAO"
_AGRUPAMENTOS_PENDENTE = {
    "dia": "SELECT P.DT_PREVISTA, {medidas} FROM P {where} GROUP BY P.DT_PREVISTA ORDER BY P.DT_PREVISTA",
    "empresa": """
        SELECT {top} P.CODEMP, EMP.NOMEFANTASIA AS EMPRESA, {medidas}
        FROM P JOIN sankhya.TSIEMP EMP ON EMP.CODEMP = P.CODEMP {where}
        GROUP BY P.CODEMP, EMP.NOMEFANTASIA ORDER BY VLR_PENDENTE DESC""",
    "ocorrencia": f"""
        SELECT {{top}} ULT.AD_CODOCOR AS CODOCOR, ISNULL(OCO.DESCROCOR, 'SEM OCORRENCIA') AS ULTIMA_OCORRENCIA, {{medidas}}
        FROM P {_ULTIMA_OCORRENCIA.format(a="P")} {{where}}
        GROUP BY ULT.AD_CODOCOR, OCO.DESCROCOR ORDER BY ITENS DESC""",
    "nota": """
        SELECT {top} P.NUNOTA, P.NUMNOTA, P.CODEMP, PAR.NOMEPARC AS PARCEIRO, MIN(P.DT_PREVISTA) AS DT_PREVISTA,
               MAX(P.ORDEMCARGA) AS ORDEMCARGA, {medidas}
        FROM P JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = P.CODPARC {where}
        GROUP BY P.NUNOTA, P.NUMNOTA, P.CODEMP, PAR.NOMEPARC ORDER BY MIN(P.DT_PREVISTA), P.NUNOTA""",
    "produto": """
        SELECT {top} P.CODPROD, PRO.DESCRPROD, {medidas}
        FROM P JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = P.CODPROD {where}
        GROUP BY P.CODPROD, PRO.DESCRPROD ORDER BY VLR_PENDENTE DESC""",
}

_MEDIDAS_ENTREGUE = """
    COUNT(DISTINCT E.NUNOTA) AS NOTAS,
    COUNT(*)                 AS ITENS,
    SUM(E.QTDNEG)            AS QTD_ENTREGUE,
    SUM(E.VLR)               AS VLR_ENTREGUE"""

_AGRUPAMENTOS_ENTREGUE = {
    "dia": """
        SELECT CAST(E.DHENTREGA AS DATE) AS DIA, {medidas} FROM E
        GROUP BY CAST(E.DHENTREGA AS DATE) ORDER BY DIA""",
    "mes": """
        SELECT CONVERT(CHAR(7), E.DHENTREGA, 120) AS MES, {medidas} FROM E
        GROUP BY CONVERT(CHAR(7), E.DHENTREGA, 120) ORDER BY MES""",
    "empresa": """
        SELECT {top} E.CODEMP, EMP.NOMEFANTASIA AS EMPRESA, {medidas}
        FROM E JOIN sankhya.TSIEMP EMP ON EMP.CODEMP = E.CODEMP
        GROUP BY E.CODEMP, EMP.NOMEFANTASIA ORDER BY VLR_ENTREGUE DESC""",
    "motorista": f"""
        SELECT {{top}} ISNULL(MOT.NOMEPARC, 'SEM MOTORISTA') AS MOTORISTA, {{medidas}}
        FROM E {_JOIN_MOTORISTA.format(a="E")}
        GROUP BY MOT.NOMEPARC ORDER BY ITENS DESC""",
    "produto": """
        SELECT {top} E.CODPROD, PRO.DESCRPROD, {medidas}
        FROM E JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = E.CODPROD
        GROUP BY E.CODPROD, PRO.DESCRPROD ORDER BY VLR_ENTREGUE DESC""",
}

_SITUACOES_REPOSICAO = ("nao_confirmada", "em_separacao", "em_entrega", "entregue", "encerrada_sem_remessa")
_SITUACOES_ENTRADA = ("pendente", "confirmada", "sem_entrada")
_MEDIDAS_SAM = """
    COUNT(*)               AS SAMS,
    SUM(S.QTD_REPOR)       AS QTD_REPOR,
    SUM(S.VLR_REPOSICAO)   AS VLR_REPOSICAO,
    SUM(S.VLR_AVARIADO)    AS VLR_AVARIADO"""

_POR_ETAPA = """
    SELECT S.SITUACAO_REPOSICAO, S.SITUACAO_ENTRADA, {medidas} FROM S
    GROUP BY S.SITUACAO_REPOSICAO, S.SITUACAO_ENTRADA ORDER BY SAMS DESC"""
_AGRUPAMENTOS_SAM = {
    "mes": """
        SELECT CONVERT(CHAR(7), S.DH_SAM, 120) AS MES, {medidas} FROM S {where}
        GROUP BY CONVERT(CHAR(7), S.DH_SAM, 120) ORDER BY MES""",
    "empresa": """
        SELECT {top} S.CODEMP, EMP.NOMEFANTASIA AS EMPRESA, {medidas}
        FROM S JOIN sankhya.TSIEMP EMP ON EMP.CODEMP = S.CODEMP {where}
        GROUP BY S.CODEMP, EMP.NOMEFANTASIA ORDER BY SAMS DESC""",
    "motorista": """
        SELECT {top} ISNULL(MOT.NOMEPARC, 'SEM MOTORISTA') AS MOTORISTA, {medidas}
        FROM S LEFT JOIN sankhya.TGFPAR MOT ON MOT.CODPARC = S.CODMOTORISTA {where}
        GROUP BY MOT.NOMEPARC ORDER BY SAMS DESC""",
    "produto": """
        SELECT {top} S.CODPROD, PRO.DESCRPROD, {medidas}
        FROM S JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = S.CODPROD {where}
        GROUP BY S.CODPROD, PRO.DESCRPROD ORDER BY SAMS DESC""",
    "sam": """
        SELECT {top} S.DH_SAM, S.CODEMP, PAR.NOMEPARC AS PARCEIRO, S.CODPROD, PRO.DESCRPROD, S.QTD_REPOR,
               S.VLR_REPOSICAO, S.VLR_AVARIADO, S.SITUACAO_REPOSICAO, S.SITUACAO_ENTRADA, S.NUNOTA_ORIGEM,
               S.NUNOTA AS NUNOTA_REPOSICAO, S.NUNOTA_REMESSA, S.DHENTREGA, S.NUNOTA_ENTRADA
        FROM S JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = S.CODPARC
               JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = S.CODPROD {where}
        ORDER BY S.DH_SAM""",
}


def _num(v) -> float:
    return round(float(v or 0), 2)


@dataclass
class Regra:
    tops: str
    ocorrencias_entregue: str
    ocorrencias_sam: str
    janela_dias: int
    dias_sem_baixa: int

    @property
    def where(self) -> str:
        return f"WHERE CAB.CODTIPOPER IN ({self.tops}) AND CAB.STATUSNOTA = 'L' AND ITE.AD_ENTREGA = 'E'"

    def entregue(self, a: str = "ITE") -> str:
        return (f"sankhya.TGFTEL T WHERE T.AD_NUNOTA = {a}.NUNOTA AND T.AD_SEQUENCIA = {a}.SEQUENCIA "
                f"AND T.AD_CODOCOR IN ({self.ocorrencias_entregue})")


def _regra(ctx: Contexto) -> Regra:
    d = ctx.regra("entregas").get("entregas", {})
    # TOPs, ocorrências e prazos são interpolados no SQL: só inteiros passam daqui.
    tops = sorted(int(t) for t in d.get("tops_remessa", []))
    entregue = sorted(int(o) for o in d.get("ocorrencias_entregue", []))
    sam = sorted(int(o) for o in d.get("ocorrencias_sam", []))
    if not tops or not entregue or not sam:
        raise PoliticaViolada("Regra de entregas sem tops_remessa, ocorrencias_entregue ou ocorrencias_sam.")
    return Regra(",".join(map(str, tops)), ",".join(map(str, entregue)), ",".join(map(str, sam)),
                 int(d.get("janela_dias", 180)), int(d.get("dias_sem_baixa", 30)))


def _validar_agrupar(agrupar: str, validos) -> None:
    if agrupar not in validos:
        raise PoliticaViolada(f"agrupar inválido. Válidos: {', '.join(validos)}.")


def _detalhe(ctx: Contexto, cte: str, modelo: str, medidas: str, params: list, top: int,
             where: str = "") -> tuple[list[dict], bool]:
    top = max(1, min(top, ctx.max_linhas))
    ranking = "{top}" in modelo
    sql = cte + modelo.format(top=f"TOP {top}", medidas=medidas, where=where)
    linhas, trunc = ctx.sql.consultar(sql, params, top if ranking else ctx.max_linhas)
    for l in linhas:
        for col in l:
            if col.startswith("VLR_"):
                l[col] = _num(l[col])
    return linhas, trunc


@pack.tool(descricao="""
O que falta entregar ao cliente: itens de entrega já faturados na remessa e ainda sem baixa. Use para
'entregas pendentes', 'o que está atrasado', 'quanto temos para entregar', 'entregas de amanhã'.
- agrupar: situacao (padrão) | dia | empresa | ocorrencia (última ocorrência do item) | nota | produto.
- situacao: atrasada | hoje | futura | sem_baixa | sem_data; filtra os agrupamentos, exceto 'situacao'.
- sem_baixa: passou de 'dias_sem_baixa' da previsão sem nenhuma ocorrência. Provavelmente foi
  entregue e não registrado; ao falar de atraso, informe separado e não some em atrasada.
- 'resumo' traz o total por situação. A previsão é a data atual do item, já reagendada.
- ITENS_COM_SAM: itens pendentes que têm SAM; o cliente ainda não recebeu ou recebeu depois do SAM.
Considera só as notas dos últimos 'janela_dias'. Pedido ainda em separação não entra.
Para uma nota ou cliente específico, use situacao_entrega.
""")
def entregas_pendentes(ctx: Contexto, codemp: int | None = None, agrupar: str = "situacao",
                       situacao: str | None = None, top: int = 50) -> dict:
    if agrupar != "situacao":
        _validar_agrupar(agrupar, ["situacao", *_AGRUPAMENTOS_PENDENTE])
    if situacao is not None and situacao not in _SITUACOES:
        raise PoliticaViolada(f"situacao inválida. Válidas: {', '.join(_SITUACOES)}.")
    r = _regra(ctx)
    filtro_emp, params = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    cte = f"""
    WITH P AS (
        SELECT CAB.NUNOTA, CAB.NUMNOTA, CAB.CODEMP, CAB.CODPARC, CAB.ORDEMCARGA, ITE.SEQUENCIA, ITE.CODPROD,
               {_SALDO} AS SALDO, {_SALDO} * {_VLR_UNIT} AS VLR,
               CAST(ITE.AD_DTENTREGA AS DATE) AS DT_PREVISTA,
               CASE WHEN OCR.SAM > 0 THEN 1 ELSE 0 END AS COM_SAM,
               CASE WHEN ITE.AD_DTENTREGA IS NULL THEN 'sem_data'
                    WHEN OCR.TOTAL = 0 AND CAST(ITE.AD_DTENTREGA AS DATE) < DATEADD(DAY, -{r.dias_sem_baixa}, {_HOJE})
                         THEN 'sem_baixa'
                    WHEN CAST(ITE.AD_DTENTREGA AS DATE) < {_HOJE} THEN 'atrasada'
                    WHEN CAST(ITE.AD_DTENTREGA AS DATE) = {_HOJE} THEN 'hoje'
                    ELSE 'futura' END AS SITUACAO
        {_DE}
        CROSS APPLY (SELECT COUNT(*) AS TOTAL,
                            SUM(CASE WHEN T.AD_CODOCOR IN ({r.ocorrencias_sam}) THEN 1 ELSE 0 END) AS SAM
                       FROM sankhya.TGFTEL T
                      WHERE T.AD_NUNOTA = ITE.NUNOTA AND T.AD_SEQUENCIA = ITE.SEQUENCIA
                        AND T.AD_CODOCOR IS NOT NULL) OCR
        {r.where}
          AND CAB.PENDENTE = 'S' AND ITE.PENDENTE = 'S' AND {_SALDO} > 0
          AND CAB.DTNEG >= DATEADD(DAY, -{r.janela_dias}, {_HOJE}){filtro_emp}
          AND NOT EXISTS (SELECT 1 FROM {r.entregue()})
    ) """
    por_situacao, _ = _detalhe(ctx, cte, _POR_SITUACAO, _MEDIDAS_PENDENTE, params, len(_SITUACOES))
    resumo = {l["SITUACAO"]: {"notas": int(l["NOTAS"] or 0), "itens": int(l["ITENS"] or 0),
                              "vlr_pendente": l["VLR_PENDENTE"], "itens_sem_carga": int(l["ITENS_SEM_CARGA"] or 0),
                              "itens_com_sam": int(l["ITENS_COM_SAM"] or 0)}
              for l in por_situacao}
    linhas, trunc = por_situacao, False
    if agrupar != "situacao":
        where = "WHERE P.SITUACAO = ?" if situacao else ""
        linhas, trunc = _detalhe(ctx, cte, _AGRUPAMENTOS_PENDENTE[agrupar], _MEDIDAS_PENDENTE,
                                 [*params, *([situacao] if situacao else [])], top, where)
    return ctx.politica.resultado(linhas, trunc, agrupar=agrupar, situacao=situacao,
                                  janela_dias=r.janela_dias, dias_sem_baixa=r.dias_sem_baixa, resumo=resumo)


@pack.tool(descricao="""
O que foi entregue ao cliente num período, pela data real da baixa. Use para 'quanto entregamos',
'entregas por dia', 'por loja', 'por motorista', 'produtos mais entregues'.
- agrupar: dia (padrão) | mes | empresa | motorista | produto.
- Motorista é o da carga em que a nota está hoje. Valor = item sem frete nem impostos da nota.
Datas em AAAA-MM-DD, período máximo de 92 dias.
""")
def entregas_realizadas(ctx: Contexto, data_inicio: str, data_fim: str, codemp: int | None = None,
                        agrupar: str = "dia", top: int = 50) -> dict:
    _validar_agrupar(agrupar, _AGRUPAMENTOS_ENTREGUE)
    ini, fim = validar_periodo(data_inicio, data_fim, max_dias=92)
    r = _regra(ctx)
    filtro_emp, p_emp = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    cte = f"""
    WITH E AS (
        SELECT CAB.NUNOTA, CAB.CODEMP, CAB.ORDEMCARGA, ITE.CODPROD, ITE.QTDNEG,
               ITE.VLRTOT - ISNULL(ITE.VLRDESC, 0) AS VLR, O.DHENTREGA
        {_DE}
        CROSS APPLY (SELECT MIN(T.DHCHAMADA) AS DHENTREGA FROM {r.entregue()}
                     HAVING MIN(T.DHCHAMADA) >= ? AND MIN(T.DHCHAMADA) < DATEADD(DAY, 1, ?)) O
        {r.where}
          AND CAB.DTNEG >= DATEADD(DAY, -{r.janela_dias}, ?) AND CAB.DTNEG < DATEADD(DAY, 1, ?){filtro_emp}
    ) """
    params = [ini, fim, ini, fim, *p_emp]
    linhas, trunc = _detalhe(ctx, cte, _AGRUPAMENTOS_ENTREGUE[agrupar], _MEDIDAS_ENTREGUE, params, top)
    total = ctx.sql.consultar(f"{cte} SELECT {_MEDIDAS_ENTREGUE} FROM E", params, 1)[0][0]
    resumo = {"notas": int(total["NOTAS"] or 0), "itens": int(total["ITENS"] or 0),
              "qtd_entregue": _num(total["QTD_ENTREGUE"]), "vlr_entregue": _num(total["VLR_ENTREGUE"])}
    return ctx.politica.resultado(linhas, trunc, periodo=f"{ini} a {fim}", agrupar=agrupar, resumo=resumo)


def _tops_sam(ctx: Contexto) -> tuple[str, str]:
    d = ctx.regra("entregas").get("entregas", {})
    reposicao = sorted(int(t) for t in d.get("tops_reposicao_sam", []))
    entrada = sorted(int(t) for t in d.get("tops_entrada_sam", []))
    if not reposicao or not entrada:
        raise PoliticaViolada("Regra de entregas sem tops_reposicao_sam ou tops_entrada_sam.")
    return ",".join(map(str, reposicao)), ",".join(map(str, entrada))


@pack.tool(descricao="""
SAM (avaria na entrega) num período, pelas duas etapas: a reposição ao cliente e a entrada do produto
avariado. Use para 'quantos SAMs', 'por loja', 'por motorista', 'produtos com mais SAM', 'reposições
que ainda não chegaram', 'entradas de avariado paradas'.
- agrupar: etapa (padrão, cruza as duas situações) | mes | empresa | motorista | produto | sam (lista).
- reposicao: nao_confirmada | em_separacao | em_entrega | entregue | encerrada_sem_remessa.
- entrada: pendente | confirmada | sem_entrada. Os filtros valem para os agrupamentos, exceto 'etapa'.
- VLR_REPOSICAO = valor reposto; VLR_AVARIADO = valor da entrada, que registra o total avariado.
Data = lançamento do SAM. Empresa e motorista = os da entrega avariada. Datas em AAAA-MM-DD, máximo de 366 dias.
Para o percentual sobre as entregas, compare com entregas_realizadas do mesmo período.
""")
def sam_entregas(ctx: Contexto, data_inicio: str, data_fim: str, codemp: int | None = None,
                 agrupar: str = "etapa", reposicao: str | None = None, entrada: str | None = None,
                 top: int = 50) -> dict:
    if agrupar != "etapa":
        _validar_agrupar(agrupar, ["etapa", *_AGRUPAMENTOS_SAM])
    if reposicao is not None and reposicao not in _SITUACOES_REPOSICAO:
        raise PoliticaViolada(f"reposicao inválida. Válidas: {', '.join(_SITUACOES_REPOSICAO)}.")
    if entrada is not None and entrada not in _SITUACOES_ENTRADA:
        raise PoliticaViolada(f"entrada inválida. Válidas: {', '.join(_SITUACOES_ENTRADA)}.")
    ini, fim = validar_periodo(data_inicio, data_fim)
    r = _regra(ctx)
    tops_reposicao, tops_entrada = _tops_sam(ctx)
    filtro_emp, p_emp = filtro_in("B.CODEMP", ctx.politica.validar_codemp(codemp))
    cte = f"""
    WITH B AS (
        SELECT R.NUNOTA, ISNULL(ORIGEM.CODEMP, R.CODEMP) AS CODEMP, R.CODPARC, RI.CODPROD, RI.QTDNEG AS QTD_REPOR, RI.VLRTOT AS VLR_REPOSICAO,
               ORIGEM.NUNOTAORIG AS NUNOTA_ORIGEM, OC.AD_CODMOTORIG AS CODMOTORISTA,
               ISNULL(OC.DHCHAMADA, R.DTNEG) AS DH_SAM,
               NOVA.NUNOTA AS NUNOTA_REMESSA, NOVA.DHENTREGA, ENT.NUNOTA AS NUNOTA_ENTRADA,
               ISNULL(ENT.VLR, 0) AS VLR_AVARIADO,
               CASE WHEN NOVA.DHENTREGA IS NOT NULL THEN 'entregue'
                    WHEN NOVA.NUNOTA IS NOT NULL THEN 'em_entrega'
                    WHEN R.STATUSNOTA <> 'L' THEN 'nao_confirmada'
                    WHEN R.PENDENTE = 'S' THEN 'em_separacao'
                    ELSE 'encerrada_sem_remessa' END AS SITUACAO_REPOSICAO,
               CASE WHEN ENT.NUNOTA IS NULL THEN 'sem_entrada'
                    WHEN ENT.STATUSNOTA = 'L' THEN 'confirmada'
                    ELSE 'pendente' END AS SITUACAO_ENTRADA
        FROM sankhya.TGFCAB R JOIN sankhya.TGFITE RI ON RI.NUNOTA = R.NUNOTA
        OUTER APPLY (SELECT TOP 1 V.NUNOTAORIG, V.SEQUENCIAORIG, C.CODEMP FROM sankhya.TGFVAR V
                       JOIN sankhya.TGFCAB C ON C.NUNOTA = V.NUNOTAORIG AND C.CODTIPOPER IN ({r.tops})
                      WHERE V.NUNOTA = RI.NUNOTA AND V.SEQUENCIA = RI.SEQUENCIA) ORIGEM
        OUTER APPLY (SELECT TOP 1 X.DHCHAMADA, X.AD_CODMOTORIG FROM sankhya.TGFTEL X
                      WHERE X.AD_NUNOTA = ORIGEM.NUNOTAORIG AND X.AD_SEQUENCIA = ORIGEM.SEQUENCIAORIG
                        AND X.AD_CODOCOR IN ({r.ocorrencias_sam})
                        AND (X.AD_NUNOTASAMREP IS NULL OR X.AD_NUNOTASAMREP = R.NUNOTA)
                      ORDER BY CASE WHEN X.AD_NUNOTASAMREP = R.NUNOTA THEN 0 ELSE 1 END, X.DHCHAMADA DESC) OC
        OUTER APPLY (SELECT TOP 1 C.NUNOTA,
                            (SELECT MIN(T.DHCHAMADA) FROM {r.entregue("V2")}) AS DHENTREGA
                       FROM sankhya.TGFVAR V
                       JOIN sankhya.TGFCAB C ON C.NUNOTA = V.NUNOTA AND C.CODTIPOPER IN ({r.tops}) AND C.STATUSNOTA = 'L'
                       CROSS APPLY (SELECT V.NUNOTA, V.SEQUENCIA) V2
                      WHERE V.NUNOTAORIG = RI.NUNOTA AND V.SEQUENCIAORIG = RI.SEQUENCIA
                      ORDER BY C.NUNOTA DESC) NOVA
        OUTER APPLY (SELECT TOP 1 C.NUNOTA, C.STATUSNOTA, EI.VLRTOT - ISNULL(EI.VLRDESC, 0) AS VLR
                       FROM sankhya.TGFVAR V
                       JOIN sankhya.TGFCAB C ON C.NUNOTA = V.NUNOTA AND C.CODTIPOPER IN ({tops_entrada})
                       JOIN sankhya.TGFITE EI ON EI.NUNOTA = V.NUNOTA AND EI.SEQUENCIA = V.SEQUENCIA
                      WHERE V.NUNOTAORIG = RI.NUNOTA AND V.SEQUENCIAORIG = RI.SEQUENCIA
                      ORDER BY C.NUNOTA DESC) ENT
        WHERE R.CODTIPOPER IN ({tops_reposicao}) AND R.DTNEG >= DATEADD(DAY, -{r.janela_dias}, ?)
    ), S AS (SELECT * FROM B WHERE B.DH_SAM >= ? AND B.DH_SAM < DATEADD(DAY, 1, ?){filtro_emp}) """
    params = [ini, ini, fim, *p_emp]
    por_etapa, _ = _detalhe(ctx, cte, _POR_ETAPA, _MEDIDAS_SAM, params, ctx.max_linhas)
    resumo = {"sams": sum(int(l["SAMS"]) for l in por_etapa), "reposicao": {}, "entrada": {},
              "vlr_reposicao": _num(sum(l["VLR_REPOSICAO"] for l in por_etapa)),
              "vlr_avariado": _num(sum(l["VLR_AVARIADO"] for l in por_etapa))}
    for l in por_etapa:
        for chave, col in (("reposicao", "SITUACAO_REPOSICAO"), ("entrada", "SITUACAO_ENTRADA")):
            resumo[chave][l[col]] = resumo[chave].get(l[col], 0) + int(l["SAMS"])
    linhas, trunc = por_etapa, False
    if agrupar != "etapa":
        conds, extra = [], []
        for col, valor in (("S.SITUACAO_REPOSICAO", reposicao), ("S.SITUACAO_ENTRADA", entrada)):
            if valor:
                conds.append(f"{col} = ?")
                extra.append(valor)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        linhas, trunc = _detalhe(ctx, cte, _AGRUPAMENTOS_SAM[agrupar], _MEDIDAS_SAM, [*params, *extra], top, where)
    return ctx.politica.resultado(linhas, trunc, periodo=f"{ini} a {fim}", agrupar=agrupar, resumo=resumo)


@pack.tool(descricao="""
Situação da entrega item a item de uma nota ou cliente: previsão, carga, motorista, data da baixa e
última ocorrência. Use para 'cadê a entrega da nota X', 'o que falta entregar para o cliente Y'.
Informe nunota, numnota (número da nota; codemp ajuda a desambiguar) ou codparc.
SITUACAO: entregue | pendente | encerrada (devolvida ou baixada sem entrega).
Só traz nota de remessa; por numnota ou codparc, olha os últimos 'janela_dias'.
""")
def situacao_entrega(ctx: Contexto, nunota: int | None = None, numnota: int | None = None,
                     codparc: int | None = None, codemp: int | None = None, top: int = 100) -> dict:
    if nunota is None and numnota is None and codparc is None:
        raise PoliticaViolada("Informe nunota, numnota ou codparc.")
    r = _regra(ctx)
    where, params = "", []
    for coluna, valor in (("CAB.NUNOTA", nunota), ("CAB.NUMNOTA", numnota), ("CAB.CODPARC", codparc)):
        if valor is not None:
            where += f" AND {coluna} = ?"
            params.append(int(valor))
    if nunota is None:
        where += f" AND CAB.DTNEG >= DATEADD(DAY, -{r.janela_dias}, {_HOJE})"
    filtro_emp, p_emp = filtro_in("CAB.CODEMP", ctx.politica.validar_codemp(codemp))
    top = max(1, min(top, ctx.max_linhas))
    sql = f"""
    SELECT TOP {top} CAB.NUNOTA, CAB.NUMNOTA, CAB.CODEMP, CAB.DTNEG, CAB.CODPARC, PAR.NOMEPARC AS PARCEIRO,
           ITE.SEQUENCIA, ITE.CODPROD, PRO.DESCRPROD, ITE.QTDNEG,
           CAST(ITE.AD_DTENTREGA AS DATE) AS DT_PREVISTA, CAB.ORDEMCARGA, MOT.NOMEPARC AS MOTORISTA,
           ENT.DHENTREGA, OCO.DESCROCOR AS ULTIMA_OCORRENCIA, ULT.DHCHAMADA AS DH_ULTIMA_OCORRENCIA,
           CASE WHEN ENT.DHENTREGA IS NOT NULL THEN 'entregue'
                WHEN ITE.PENDENTE = 'S' AND {_SALDO} > 0 THEN 'pendente'
                ELSE 'encerrada' END AS SITUACAO
    {_DE}
    JOIN sankhya.TGFPAR PAR ON PAR.CODPARC = CAB.CODPARC
    JOIN sankhya.TGFPRO PRO ON PRO.CODPROD = ITE.CODPROD
    {_JOIN_MOTORISTA.format(a="CAB")}
    OUTER APPLY (SELECT MIN(T.DHCHAMADA) AS DHENTREGA FROM {r.entregue()}) ENT
    {_ULTIMA_OCORRENCIA.format(a="ITE")}
    {r.where}{where}{filtro_emp}
    ORDER BY CAB.DTNEG DESC, CAB.NUNOTA DESC, ITE.SEQUENCIA"""
    linhas, trunc = ctx.sql.consultar(sql, [*params, *p_emp], top)
    return ctx.politica.resultado(linhas, trunc, janela_dias=None if nunota is not None else r.janela_dias)
