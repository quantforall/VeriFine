"""
Quant4all — Métricas de rentabilidad y riesgo (§14 de la spec).

Todo se calcula sobre la serie de retornos diarios encadenados, nunca sobre
el NAV. El drawdown sobre el índice TWR: sobre el patrimonio, una
transferencia grande aparece como una caída que nunca existió.

El tiempo se cuenta de DOS maneras distintas y no son intercambiables:
    CAGR         -> calendario (días naturales / 365,25)
    volatilidad  -> observaciones (√252)

Contar el CAGR con N/252 estaba mal: con 261 sesiones reales al año, una
ventana de 12 meses exactos daba un "anualizado" menor que el acumulado.
"""

from __future__ import annotations

import math
import datetime as dt
from dataclasses import dataclass, asdict

P_ANNUAL = 252
DAYS_YEAR = 365.25

# Un año natural mide 364-366 días y una ventana móvil arranca en la primera
# sesión disponible, así que "un año" cae a menudo en 0,99. Anular la
# anualización por dos días sería absurdo; el umbral protege de períodos
# realmente cortos (un YTD de 0,62 sigue quedando fuera).
MIN_ANNUALIZE = 0.95

_ES_MONTH = {1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
             7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic"}


def _years(start: str, end: str) -> float:
    d0 = dt.datetime.strptime(start, "%Y%m%d")
    d1 = dt.datetime.strptime(end, "%Y%m%d")
    return (d1 - d0).days / DAYS_YEAR


@dataclass
class Metrics:
    n: int
    years: float
    annualizable: bool
    total: float                 # acumulado, en %
    cagr: float | None           # None si years < 1 (§14.3)
    vol: float
    vol_down: float
    sharpe: float | None
    sortino: float | None
    max_dd: float
    calmar: float | None
    var95: float
    cvar95: float
    pct_positive: float
    best_day: float
    worst_day: float
    rf_used: float

    def as_dict(self) -> dict:
        return asdict(self)


def compute(returns: list[float], start: str, end: str,
            rf: float = 0.0, p: int = P_ANNUAL) -> Metrics:
    """rf es la tasa libre de riesgo ANUAL, en tanto por uno.

    No tiene valor por defecto útil: con rf=0 el Sharpe sale inflado en torno
    a 0,10–0,15. Se expone en la salida (`rf_used`) para que ninguna pantalla
    muestre un Sharpe sin decir contra qué se ha calculado.
    """
    n = len(returns)
    if n < 2:
        raise ValueError(f"Serie insuficiente: {n} retornos")

    years = _years(start, end)
    annualizable = years >= MIN_ANNUALIZE

    # acumulado e índice
    idx, c = [1.0], 1.0
    for r in returns:
        c *= 1 + r
        idx.append(c)
    total = c - 1

    cagr = (1 + total) ** (1 / years) - 1 if annualizable and years > 0 else None

    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    vol = math.sqrt(var * p)
    vol_down = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / n * p)

    sharpe = (cagr - rf) / vol if (cagr is not None and vol > 0) else None
    sortino = (cagr - rf) / vol_down if (cagr is not None and vol_down > 0) else None

    peak, mdd = -math.inf, 0.0
    for v in idx:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)

    calmar = (cagr / abs(mdd)) if (cagr is not None and mdd < 0) else None

    # Percentil por índice inferior: es la convención con la que se fijaron
    # los golden values de §13.2.
    s = sorted(returns)
    k = max(int(0.05 * n), 1)
    var95 = s[k]
    cvar95 = sum(s[:k]) / k

    return Metrics(
        n=n, years=years, annualizable=annualizable,
        total=total * 100, cagr=cagr * 100 if cagr is not None else None,
        vol=vol * 100, vol_down=vol_down * 100,
        sharpe=sharpe, sortino=sortino,
        max_dd=mdd * 100, calmar=calmar,
        var95=var95 * 100, cvar95=cvar95 * 100,
        pct_positive=100 * sum(1 for r in returns if r > 0) / n,
        best_day=max(returns) * 100, worst_day=min(returns) * 100,
        rf_used=rf * 100,
    )


def from_series(series, rf: float = 0.0) -> Metrics:
    """Atajo sobre un q4_engine.Series."""
    return compute(series.returns, series.dates[0], series.dates[-1], rf=rf)


# --------------------------------------------------------------------------
# Descomposición del riesgo por divisa (§14.4)
# --------------------------------------------------------------------------

@dataclass
class RiskSplit:
    vol_total: float
    vol_strategy: float
    vol_fx: float
    correlation: float
    share_strategy: float
    share_fx: float
    share_cross: float


def decompose_risk(r_total: list[float], r_strategy: list[float],
                   p: int = P_ANNUAL) -> RiskSplit:
    """Sobre log-retornos, l_total = l_estrategia + l_divisa es exacta.

    Var(total) = Var(estr) + Var(fx) + 2·Cov(estr, fx)
    """
    n = min(len(r_total), len(r_strategy))
    lt = [math.log(1 + x) for x in r_total[:n]]
    ls = [math.log(1 + x) for x in r_strategy[:n]]
    lf = [a - b for a, b in zip(lt, ls)]

    def v(x):
        m = sum(x) / len(x)
        return sum((i - m) ** 2 for i in x) / (len(x) - 1)

    def cov(x, y):
        mx, my = sum(x) / len(x), sum(y) / len(y)
        return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (len(x) - 1)

    vt, vs, vf = v(lt), v(ls), v(lf)
    cv = cov(ls, lf)
    return RiskSplit(
        vol_total=math.sqrt(vt * p) * 100,
        vol_strategy=math.sqrt(vs * p) * 100,
        vol_fx=math.sqrt(vf * p) * 100,
        correlation=cv / math.sqrt(vs * vf) if vs > 0 and vf > 0 else float("nan"),
        share_strategy=100 * vs / vt, share_fx=100 * vf / vt,
        share_cross=100 * 2 * cv / vt,
    )


# --------------------------------------------------------------------------
# Ventanas móviles (§14.5)
# --------------------------------------------------------------------------

STANDARD_WINDOWS = [("1 año", 1), ("3 años", 3), ("5 años", 5)]


def trailing_windows(dates: list[str], windows=None) -> list[dict]:
    """Devuelve una entrada por ventana, VACÍA si no hay histórico suficiente.

    §14.5 — una ventana sin datos se muestra vacía y con la razón escrita al
    lado. No se oculta la fila (haría pensar que la ventana no existe en el
    producto) ni se rellena con el período disponible más largo (sería una
    mentira).
    """
    windows = windows or STANDARD_WINDOWS
    end = dates[-1]
    e = dt.datetime.strptime(end, "%Y%m%d")
    out = []
    for label, yrs in windows:
        try:
            s = e.replace(year=e.year - yrs)
        except ValueError:
            s = e.replace(year=e.year - yrs, day=28)
        start = s.strftime("%Y%m%d")
        if start < dates[0]:
            out.append(dict(label=label, available=False,
                            needs=_ES_MONTH[s.month] + " " + str(s.year), start=None, end=None))
        else:
            first = next(d for d in dates if d >= start)
            out.append(dict(label=label, available=True, start=first, end=end))
    yrs_total = _years(dates[0], end)
    out.append(dict(label=f"Total · {yrs_total:.2f} años", available=True,
                    start=dates[0], end=end))
    return out
