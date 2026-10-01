"""Cron de 5 campos (min hora dia mês dia_semana). Suporta *, listas, faixas e passos.

Dia da semana: 0 ou 7 = domingo, 1 = segunda ... 6 = sábado.
Se dia do mês E dia da semana forem restritos, vale qualquer um dos dois (padrão cron).
"""
from datetime import datetime

_LIMITES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _campo(expr: str, lo: int, hi: int) -> set[int]:
    valores: set[int] = set()
    for parte in expr.split(","):
        base, _, passo_txt = parte.partition("/")
        passo = int(passo_txt) if passo_txt else 1
        if passo < 1:
            raise ValueError(f"passo inválido: {parte}")
        if base == "*":
            ini, fim = lo, hi
        elif "-" in base:
            ini, fim = (int(x) for x in base.split("-", 1))
        else:
            ini = fim = int(base)
            if passo_txt:
                fim = hi
        if not (lo <= ini <= fim <= hi):
            raise ValueError(f"valor fora da faixa {lo}-{hi}: {parte}")
        valores.update(range(ini, fim + 1, passo))
    return valores


class Cron:
    def __init__(self, expr: str):
        partes = expr.split()
        if len(partes) != 5:
            raise ValueError(f"cron precisa de 5 campos: '{expr}'")
        self.expr = expr
        (self.minutos, self.horas, self.dias, self.meses, dsem) = (
            _campo(p, lo, hi) for p, (lo, hi) in zip(partes, _LIMITES)
        )
        self.dias_semana = {0 if d == 7 else d for d in dsem}
        self._dia_restrito = partes[2] != "*"
        self._dsem_restrito = partes[4] != "*"

    def bate(self, t: datetime) -> bool:
        if t.minute not in self.minutos or t.hour not in self.horas or t.month not in self.meses:
            return False
        dsem = (t.weekday() + 1) % 7  # Python: segunda=0 -> cron: segunda=1, domingo=0
        dia_ok, dsem_ok = t.day in self.dias, dsem in self.dias_semana
        if self._dia_restrito and self._dsem_restrito:
            return dia_ok or dsem_ok
        return dia_ok and dsem_ok
