"""
Quant4all — Motor de TWR y atribución (§4–§7 de la spec).

Una sola función de encadenamiento, parametrizada por la matriz de tipos de
cambio. Se invoca dos veces: con la matriz real y con la matriz congelada.
No existen rutas de código separadas para EUR y USD, ni para real y
FX-neutral. Esa es la condición que hace que T3, T4 y T5 sean verificaciones
reales y no tautologías.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from q4_parser import (Dataset, fx_matrix, currency_buckets, flows,
                       accruals, nav_series)

W_FLOW = 1.0        # §4 — calibrado: flujo a inicio de día. Reproduce IBKR a 0,00 pb.


class UndefinedReturn(ValueError):
    pass


# --------------------------------------------------------------------------
# Tipos de cambio
# --------------------------------------------------------------------------

class FX:
    """Matriz de tipos con arrastre del último valor conocido.

    `frozen_at` congela toda la matriz en una fecha: es el escenario
    FX-neutral de §7.1, sin motor paralelo.
    """

    def __init__(self, matrix: dict[str, dict[str, float]], base: str,
                 frozen_at: str | None = None):
        self.m, self.base, self.frozen = matrix, base, frozen_at

    def rate(self, currency: str, date: str) -> float:
        if currency == self.base:
            return 1.0
        d = self.frozen or date
        series = self.m.get(currency)
        if not series:
            raise KeyError(f"Sin tipos de cambio para {currency}")
        if d in series:
            return series[d]
        prev = [k for k in series if k <= d]
        if prev:
            return series[max(prev)]
        nxt = [k for k in series if k > d]
        if nxt:
            return series[min(nxt)]
        raise KeyError(f"Sin tipo de cambio para {currency} en {d}")


# --------------------------------------------------------------------------
# Serie de retornos
# --------------------------------------------------------------------------

@dataclass
class Series:
    dates: list[str]
    returns: list[float]
    values: list[float]

    def total(self) -> float:
        p = 1.0
        for r in self.returns:
            p *= 1 + r
        return p - 1

    def index(self, base: float = 100.0) -> list[float]:
        out, c = [base], base
        for r in self.returns:
            c *= 1 + r
            out.append(c)
        return out

    def max_drawdown(self) -> float:
        peak, mdd = -math.inf, 0.0
        for v in self.index():
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        return mdd


@dataclass
class SeriesInputs:
    """Piezas de `build_series()` que dependen SOLO de `(dataset, cuentas)`
    — nunca de `start`/`end` ni de la divisa de análisis (la conversión de
    divisa pasa DESPUÉS, dentro de `build_series()`, vía `fx.rate(m, d)`).

    Calcularlas una vez y reutilizarlas entre llamadas es lo que evita
    recorrer el histórico ENTERO una vez por año/ventana en
    `years_table()`/`trailing_table()` (app.py) — ver `precompute_series_inputs()`."""
    fx_matrix: dict[str, dict[str, float]]
    navs: dict[str, float]
    buckets: dict[str, dict[str, float]]
    accr: dict[str, float]
    flows: dict[str, list[tuple[str, float]]]


def precompute_series_inputs(ds: Dataset, accounts: list[str] | None = None) -> SeriesInputs:
    """Calcula de una vez las cinco piezas de `build_series()`/`attribute()`
    que no dependen del periodo pedido.

    `attribute()` llama a `build_series()` dos veces (real y "congelada",
    §7.1) — sin esto, cada una recalculaba las cinco por su cuenta, aunque
    fueran idénticas salvo `buckets` (que sólo hace falta en la congelada).
    `years_table()`/`trailing_table()` (app.py) llaman a `attribute()` una
    vez por año/ventana con el MISMO dataset y cuentas — pasar aquí el
    resultado de una sola llamada evita repetirlo una vez por cada una."""
    return SeriesInputs(
        fx_matrix=fx_matrix(ds),
        navs=nav_series(ds, accounts),
        buckets=currency_buckets(ds, accounts),
        accr=accruals(ds, accounts),
        flows=flows(ds, accounts),
    )


def build_series(ds: Dataset, start: str, end: str,
                 analysis_currency: str | None = None,
                 accounts: list[str] | None = None,
                 fx_frozen_at: str | None = None,
                 from_buckets: bool = False,
                 w: float = W_FLOW,
                 precomputed: SeriesInputs | None = None) -> Series:
    """Construye la serie de retornos diarios.

    from_buckets=False -> valor = NAV oficial de IBKR, convertido a la moneda
                          de análisis. Es la serie Total.
    from_buckets=True  -> valor = suma de los cubos por divisa revalorados con
                          la matriz fx dada. Con fx_frozen_at, es la
                          Estrategia (§7.1).

    `precomputed`: resultado de `precompute_series_inputs(ds, accounts)` ya
    calculado por quien llama (típicamente `attribute()`, o app.py entre
    varias llamadas) — si no se da, se calcula aquí igual que siempre. Sólo
    es un atajo para no repetir trabajo; el resultado es idéntico en ambos
    casos porque son las mismas cinco funciones sobre el mismo `(ds, accounts)`.
    """
    base = ds.base_currency
    m = analysis_currency or base
    inputs = precomputed or precompute_series_inputs(ds, accounts)
    fx = FX(inputs.fx_matrix, base, frozen_at=fx_frozen_at)

    navs = inputs.navs
    buckets = inputs.buckets if from_buckets else None
    accr = inputs.accr
    fl = inputs.flows

    dates = [d for d in sorted(navs) if start <= d <= end]
    if len(dates) < 2:
        raise ValueError(f"Serie insuficiente entre {start} y {end}")

    def value(d: str) -> float:
        if from_buckets:
            b = buckets.get(d)
            if b is None:
                raise KeyError(f"Sin composición por divisa para {d}")
            v = sum(a * fx.rate(c, d) for c, a in b.items()) + accr.get(d, 0.0)
        else:
            v = navs[d]
        return v / fx.rate(m, d)

    def flow(d: str) -> float:
        # §6 — cada flujo se convierte con el FX de SU PROPIA fecha.
        return sum(a * fx.rate(c, d) for c, a in fl.get(d, [])) / fx.rate(m, d)

    rets, vals = [], [value(dates[0])]
    for prev, cur in zip(dates, dates[1:]):
        v0, v1, f = vals[-1], value(cur), flow(cur)
        den = v0 + w * f
        if den <= 0:
            # §9 — retorno indefinido. No devolver 0 en silencio.
            raise UndefinedReturn(f"Denominador <= 0 en {cur}: V0={v0:.2f} F={f:.2f}")
        rets.append((v1 - v0 - f) / den)
        vals.append(v1)
    return Series(dates, rets, vals)


# --------------------------------------------------------------------------
# Atribución
# --------------------------------------------------------------------------

@dataclass
class Attribution:
    total: float
    strategy: float
    fx: float
    series_total: Series
    series_strategy: Series

    def closes(self) -> float:
        return (1 + self.strategy) * (1 + self.fx) - 1 - self.total


def attribute(ds: Dataset, start: str, end: str,
              analysis_currency: str | None = None,
              accounts: list[str] | None = None,
              precomputed: SeriesInputs | None = None) -> Attribution:
    """§7 — Estrategia por simulación FX-neutral, divisa por RESIDUO.

    Definir la divisa como residuo garantiza por construcción que el desglose
    cierre. Calcular ambos componentes por separado dejaría un término cruzado
    sin atribuir y T5 fallaría por unos puntos básicos sin que eso indicara
    ningún error real.

    Las dos llamadas a `build_series()` de abajo (real y congelada) comparten
    el mismo `(ds, accounts)` — por eso `precompute_series_inputs()` se
    calcula UNA vez aquí (o se reutiliza el de `precomputed`, si quien llama
    ya lo tenía de una llamada anterior con el mismo dataset/cuentas — ver
    `app.py::_cached_series_inputs`) y se pasa a ambas, en vez de que cada
    una repita el mismo trabajo por su cuenta.
    """
    inputs = precomputed or precompute_series_inputs(ds, accounts)
    st = build_series(ds, start, end, analysis_currency, accounts, precomputed=inputs)
    sn = build_series(ds, start, end, analysis_currency, accounts,
                      fx_frozen_at=start, from_buckets=True, precomputed=inputs)
    total, strat = st.total(), sn.total()
    return Attribution(total=total, strategy=strat,
                       fx=(1 + total) / (1 + strat) - 1,
                       series_total=st, series_strategy=sn)
