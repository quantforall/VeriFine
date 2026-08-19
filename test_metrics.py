"""Tests de la capa de métricas (§13.2, §13.4, §14): riesgo y ventanas móviles.

Regresión congelada sobre la serie Estrategia (FX-neutral). Los valores se
anclan a la salida actual del motor.

NOTA sobre Sharpe / Sortino: §13.2 imprimió 0,62 / 0,75 / 0,92 … calculando el
CAGR con el convenio antiguo N/252. §14.3 lo corrigió a calendario
(días/365,25), que es lo que usa q4_metrics, así que el Sharpe sale más alto.
No es un error: el motor es el correcto y §13.2 lleva el convenio superado.
Vol, Vol bajista, MaxDD, VaR y §13.4 sí casan con la tabla impresa.
"""

from __future__ import annotations

import pytest

import q4_engine as E
import q4_metrics as M

TOL = 0.02          # tolerancia en puntos porcentuales para vol/dd/var…
TOL_RATIO = 0.005   # para ratios adimensionales (Sharpe, Sortino, correlación)

PERIODS = [
    ("2023", "20221230", "20231229"),
    ("2024", "20231229", "20241231"),
    ("2025", "20241231", "20251231"),
    ("2026", "20251231", "20260813"),
]

# Serie Estrategia (fx congelado al inicio del período). Ancladas al motor.
# vol / vol_down / max_dd / var95 / pct_pos casan con §13.2.
# sharpe / sortino usan CAGR calendario (§14.3), no el N/252 de §13.2.
GOLDEN_RISK = {
    "2023": dict(vol=15.453418, vol_down=11.039501, sharpe=0.631501,
                 sortino=0.883993, max_dd=-14.842793, var95=-1.633421,
                 pct_pos=56.153846),
    "2024": dict(vol=27.240255, vol_down=18.167820, sharpe=0.780573,
                 sortino=1.170366, max_dd=-24.536217, var95=-2.294128,
                 pct_pos=51.908397),
    "2025": dict(vol=22.297655, vol_down=16.059043, sharpe=0.750451,
                 sortino=1.041986, max_dd=-20.129699, var95=-2.356185,
                 pct_pos=53.639847),
    "2026": dict(vol=32.826401, vol_down=21.848166, sharpe=None,
                 sortino=None, max_dd=-14.544668, var95=-2.850516,
                 pct_pos=57.763975),
}


def _strategy_metrics(ds, start, end):
    s = E.build_series(ds, start, end, fx_frozen_at=start, from_buckets=True)
    return M.from_series(s)


@pytest.mark.parametrize("label,start,end", PERIODS)
def test_risk_by_year(dataset, label, start, end):
    m = _strategy_metrics(dataset, start, end)
    exp = GOLDEN_RISK[label]
    assert abs(m.vol - exp["vol"]) <= TOL, f"{label}: vol"
    assert abs(m.vol_down - exp["vol_down"]) <= TOL, f"{label}: vol bajista"
    assert abs(m.max_dd - exp["max_dd"]) <= TOL, f"{label}: max dd"
    assert abs(m.var95 - exp["var95"]) <= TOL, f"{label}: var95"
    assert abs(m.pct_positive - exp["pct_pos"]) <= TOL, f"{label}: %días+"
    if exp["sharpe"] is None:
        assert m.sharpe is None and m.sortino is None, f"{label}: 2026 YTD no anualizable"
    else:
        assert abs(m.sharpe - exp["sharpe"]) <= TOL_RATIO, f"{label}: sharpe"
        assert abs(m.sortino - exp["sortino"]) <= TOL_RATIO, f"{label}: sortino"


def test_partial_period_not_annualized(dataset):
    """§14.3 — el 2026 YTD (Y<1) muestra acumulado pero no CAGR/Sharpe."""
    m = M.from_series(E.build_series(dataset, "20251231", "20260813"))
    assert m.years < 1.0
    assert m.annualizable is False
    assert m.cagr is None and m.sharpe is None and m.sortino is None


# --------------------------------------------------------------------------
# §13.4 — descomposición del riesgo por divisa (histórico)
# --------------------------------------------------------------------------

def test_risk_decomposition(dataset):
    d0, d1 = dataset.dates[0], dataset.dates[-1]
    rt = E.build_series(dataset, d0, d1).returns
    rs = E.build_series(dataset, d0, d1, fx_frozen_at=d0, from_buckets=True).returns
    r = M.decompose_risk(rt, rs)
    assert abs(r.vol_total - 24.676814) <= TOL          # §13.4: 24,68
    assert abs(r.vol_strategy - 24.340029) <= TOL       # §13.4: 24,34
    assert abs(r.vol_fx - 6.531013) <= TOL              # §13.4: 6,53
    assert abs(r.correlation - (-0.082238)) <= TOL_RATIO   # §13.4: -0,082
    assert abs(r.share_strategy - 97.289057) <= 0.1     # §13.4: 97,3
    assert abs(r.share_fx - 7.004594) <= 0.1            # §13.4: +7,0
    assert abs(r.share_cross - (-4.293651)) <= 0.1      # §13.4: -4,3


# --------------------------------------------------------------------------
# §14.5 — ventanas móviles y regla de vacíos
# --------------------------------------------------------------------------

def test_trailing_windows_5y_empty(dataset):
    """La ventana de 5 años no tiene histórico suficiente: vacía y explícita."""
    ws = {w["label"]: w for w in M.trailing_windows(dataset.dates)}
    assert ws["5 años"]["available"] is False
    assert ws["5 años"]["needs"]                        # razón escrita al lado
    assert ws["1 año"]["available"] is True
    assert ws["3 años"]["available"] is True


def test_t11_one_year_cagr_equals_cumulative(dataset):
    """T11 — en una ventana de 1 año, CAGR ≈ acumulado (falla con N/252)."""
    ws = {w["label"]: w for w in M.trailing_windows(dataset.dates)}
    w1 = ws["1 año"]
    m = _strategy_metrics(dataset, w1["start"], w1["end"])
    # Con calendario, Y≈1 → CAGR ≈ acumulado (Δ de pocos pb por el sobrante de
    # días). El convenio N/252 daría ~2 pp menos y reventaría esta tolerancia.
    assert m.cagr is not None
    assert abs(m.cagr - m.total) <= 0.2, \
        f"CAGR {m.cagr:.4f}% vs acumulado {m.total:.4f}%"
