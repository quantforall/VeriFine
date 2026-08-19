"""Tests dorados (§10, §13): el motor debe reproducir §13 desde los XML crudos.

Regresión congelada (T10). Los valores de referencia se anclan a lo que el
motor produce HOY, no a las cifras redondeadas de la tabla §13, para que
cualquier refactor que mueva un resultado rompa el build.

Dos anclas difieren a propósito de la tabla impresa en §13.1, y está
documentado el porqué:

  * Estrategia y divisa de 2023 (y del histórico): §13.1 lista 9,9341 % /
    -2,2160 %; el motor da 9,7239 % / -2,0286 % (~22 pb). RESUELTO con el
    backfill real 2021→2026: el 9,9341 % era un ARTEFACTO. Venía de un script
    previo que escalaba los cubos del día de arranque (20221230) para cuadrar
    el NAV, porque en los ficheros de ~/Downloads ese día era un bootstrap SIN
    ConversionRate. Con el backfill, 20221230 trae su ConversionRate real
    (0,934240) y reconstruye el NAV exacto: el escalado es inerte y la
    estrategia correcta es ~9,72 %. El motor tenía razón; la cifra de §13.1
    está mal y debería corregirse a ~9,72 % / ~-2,03 %.
"""

from __future__ import annotations

import pytest

import q4_engine as E
import q4_parser as P

# Tolerancias en puntos básicos: Δpb = |got - exp| * 100  (got/exp en %).
TOL_TWR_PB = 1.0        # T1: reproducción del TWR de IBKR
TOL_REG_PB = 1.0        # T10: congelación de regresión

# (label, start, end)
PERIODS = [
    ("2023", "20221230", "20231229"),
    ("2024", "20231229", "20241231"),
    ("2025", "20241231", "20251231"),
    ("2026", "20251231", "20260813"),
]

# TWR EUR oficial de IBKR (§13.1). Se reproduce a 0,0000 pb (T1).
GOLDEN_TWR_EUR = {
    "2023": 7.497979,
    "2024": 29.524351,
    "2025": 2.879392,
    "2026": 39.087122,
}

# Atribución congelada del motor (T10). twr_usd / strat / fx en %.
# NOTA 2023: strat/fx difieren de §13.1 por el bootstrap (ver docstring).
GOLDEN_ATTR = {
    "2023": dict(twr_usd=11.603496, strat=9.723875, fx=-2.028634),
    "2024": dict(twr_usd=21.530173, strat=21.439144, fx=6.657826),
    "2025": dict(twr_usd=16.745021, strat=16.720929, fx=-11.858659),
    "2026": dict(twr_usd=36.522957, strat=36.524572, fx=1.876988),
}


def _pb(got_pct: float, exp_pct: float) -> float:
    return abs(got_pct - exp_pct) * 100.0


# --------------------------------------------------------------------------
# Sanidad del dataset
# --------------------------------------------------------------------------

def test_dataset_shape(dataset):
    """Serie continua 2022-12-30 → 2026-08-13, 945 fechas, una cuenta, sin conflictos."""
    assert dataset.dates[0] == "20221230"
    assert dataset.dates[-1] == "20260813"
    assert len(dataset.dates) == 945
    assert dataset.accounts == ["U7790974"]
    assert dataset.nav_conflicts == []
    assert dataset.bootstrapped == ["20221230"]


# --------------------------------------------------------------------------
# T1 — TWR EUR contra IBKR
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label,start,end", PERIODS)
def test_t1_twr_eur(dataset, label, start, end):
    twr = E.build_series(dataset, start, end).total() * 100
    assert _pb(twr, GOLDEN_TWR_EUR[label]) <= TOL_TWR_PB, \
        f"{label}: TWR EUR {twr:.6f}% vs golden {GOLDEN_TWR_EUR[label]:.6f}%"


# --------------------------------------------------------------------------
# T10 — atribución congelada (Estrategia / Divisa / USD)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label,start,end", PERIODS)
def test_attribution_frozen(dataset, label, start, end):
    at = E.attribute(dataset, start, end)
    au = E.attribute(dataset, start, end, analysis_currency="USD")
    exp = GOLDEN_ATTR[label]
    assert _pb(au.total * 100, exp["twr_usd"]) <= TOL_REG_PB, f"{label}: TWR USD"
    assert _pb(at.strategy * 100, exp["strat"]) <= TOL_REG_PB, f"{label}: Estrategia"
    assert _pb(at.fx * 100, exp["fx"]) <= TOL_REG_PB, f"{label}: Divisa"


# --------------------------------------------------------------------------
# T4 — invariancia de la estrategia respecto a la moneda de análisis
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label,start,end", PERIODS)
def test_t4_strategy_invariance(dataset, label, start, end):
    at = E.attribute(dataset, start, end)
    au = E.attribute(dataset, start, end, analysis_currency="USD")
    assert abs(at.strategy - au.strategy) <= 1e-11, \
        f"{label}: estrategia EUR≠USD ({abs(at.strategy - au.strategy):.2e})"


# --------------------------------------------------------------------------
# T5 — cierre multiplicativo de la atribución
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label,start,end", PERIODS)
def test_t5_multiplicative_closure(dataset, label, start, end):
    at = E.attribute(dataset, start, end)
    assert abs(at.closes()) <= 1e-12, f"{label}: cierre {at.closes():.2e}"


# --------------------------------------------------------------------------
# T3 — identidad sin flujos (2026 no tuvo flujos)
# --------------------------------------------------------------------------

def test_t3_no_flow_identity_2026(dataset):
    fx = E.FX(P.fx_matrix(dataset), dataset.base_currency)
    re_ = E.build_series(dataset, "20251231", "20260813").total()
    ru = E.build_series(dataset, "20251231", "20260813",
                        analysis_currency="USD").total()
    closed = (1 + re_) * fx.rate("USD", "20251231") / fx.rate("USD", "20260813") - 1
    assert _pb(ru * 100, closed * 100) <= TOL_TWR_PB, \
        f"cadena {ru*100:.6f}% vs forma cerrada {closed*100:.6f}%"


# --------------------------------------------------------------------------
# T6 — composición por bloques
# --------------------------------------------------------------------------

def test_t6_block_composition(dataset):
    sub = [("20241231", "20250630"), ("20250630", "20251231")]
    prod = 1.0
    for a, b in sub:
        prod *= 1 + E.build_series(dataset, a, b).total()
    whole = 1 + E.build_series(dataset, "20241231", "20251231").total()
    assert abs(prod - whole) <= 1e-9, f"bloques {prod:.10f} vs entero {whole:.10f}"


# --------------------------------------------------------------------------
# Histórico completo
# --------------------------------------------------------------------------

def test_historico(dataset):
    d0, d1 = dataset.dates[0], dataset.dates[-1]
    full = E.attribute(dataset, d0, d1)
    fu = E.attribute(dataset, d0, d1, analysis_currency="USD")

    assert _pb(full.total * 100, 99.235643) <= TOL_REG_PB          # §13: 99,24
    # Estrategia histórica: congelada al día bootstrap → 113,389 %.
    # §13.1 lista 113,80 % (~41 pb, misma causa que 2023).
    assert _pb(full.strategy * 100, 113.389084) <= TOL_REG_PB
    assert abs(full.strategy - fu.strategy) <= 1e-11               # T4 histórico
    assert abs(full.closes()) <= 1e-12                             # T5 histórico

    mdd_total = full.series_total.max_drawdown() * 100
    mdd_strat = full.series_strategy.max_drawdown() * 100
    assert _pb(mdd_total, -25.887232) <= TOL_REG_PB                # §13.2: -25,89
    assert _pb(mdd_strat, -24.396455) <= TOL_REG_PB                # §13.2: -24,40
