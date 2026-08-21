"""Tests de q4_engine.py con datos sintéticos (no dependen del fixture
dorado de test_golden.py/conftest.py, así que corren siempre).

Foco: `precompute_series_inputs()` / el parámetro `precomputed=` de
`build_series()`/`attribute()` deben dar EXACTAMENTE el mismo resultado que
antes de esa refactorización (calcular las cinco piezas una vez y
reutilizarlas) — no es un cambio de comportamiento, sólo de cuántas veces se
recalculan las mismas cinco funciones sobre el mismo `(dataset, cuentas)`."""

from __future__ import annotations

import pandas as pd
import pytest

import q4_engine as E
import q4_parser as P


def _synthetic_dataset() -> P.Dataset:
    """Dos cuentas, dos divisas, ~30 días — lo justo para que attribute()
    tenga una serie no trivial (flujos, FX, varias cuentas)."""
    dates = [f"2026010{d}" if d < 10 else f"202601{d}" for d in range(1, 21)]
    accounts = ["U1", "U2"]
    nav_rows, cash_rows, pos_rows, fx_rows, mov_rows = [], [], [], [], []
    val = {"U1": 100000.0, "U2": 50000.0}
    for i, d in enumerate(dates):
        for a in accounts:
            val[a] *= 1 + (0.002 if a == "U1" else -0.001) * (1 if i % 3 else -1)
            nav_rows.append(dict(account=a, date=d, total=val[a], accruals=0.0,
                                 cash_base=val[a] * 0.2, currency="EUR"))
            cash_rows.append(dict(account=a, date=d, currency="EUR", balance_local=val[a] * 0.2))
            cash_rows.append(dict(account=a, date=d, currency="USD", balance_local=val[a] * 0.1))
            pos_rows.append(dict(account=a, date=d, conid="1", symbol="AAA",
                                 asset_category="STK", currency="USD", quantity=10,
                                 price=100.0, value_local=val[a] * 0.7,
                                 description="x", cost_basis_price=90,
                                 cost_basis_money=900, fifo_pnl_unrealized=10))
        fx_rows.append(dict(date=d, currency="USD", to="EUR", rate=1.1))
    mov_rows.append(dict(account="U1", movement_id="TX1", date=dates[5], settle_date=dates[5],
                         type="Deposits/Withdrawals", currency="EUR", symbol=None, conid=None,
                         amount_local=2000.0, fx_to_base=1.0, flow_base=2000.0,
                         is_flow=True, counterparty=None, source="cash"))
    fx = pd.DataFrame(fx_rows)
    fx = fx[fx["to"] == "EUR"].drop(columns=["to"])
    return P.Dataset(nav=pd.DataFrame(nav_rows), positions=pd.DataFrame(pos_rows),
                     cash=pd.DataFrame(cash_rows), fx=fx, movements=pd.DataFrame(mov_rows),
                     base_currency="EUR")


@pytest.fixture
def ds():
    return _synthetic_dataset()


def test_attribute_identical_with_and_without_precomputed(ds):
    d0, d1 = ds.dates[0], ds.dates[-1]
    a = E.attribute(ds, d0, d1)
    inputs = E.precompute_series_inputs(ds, None)
    b = E.attribute(ds, d0, d1, precomputed=inputs)
    assert a.total == pytest.approx(b.total)
    assert a.strategy == pytest.approx(b.strategy)
    assert a.fx == pytest.approx(b.fx)
    assert a.series_total.returns == pytest.approx(b.series_total.returns)
    assert a.series_strategy.returns == pytest.approx(b.series_strategy.returns)


def test_build_series_identical_with_and_without_precomputed(ds):
    d0, d1 = ds.dates[0], ds.dates[-1]
    inputs = E.precompute_series_inputs(ds, None)
    for from_buckets, frozen in ((False, None), (True, d0)):
        s1 = E.build_series(ds, d0, d1, from_buckets=from_buckets, fx_frozen_at=frozen)
        s2 = E.build_series(ds, d0, d1, from_buckets=from_buckets, fx_frozen_at=frozen,
                            precomputed=inputs)
        assert s1.returns == pytest.approx(s2.returns)
        assert s1.values == pytest.approx(s2.values)


def test_attribute_reuses_precomputed_bundle_across_different_periods(ds, monkeypatch):
    """El motivo real de precompute_series_inputs(): dos ventanas DISTINTAS
    del mismo dataset/cuentas comparten el mismo bundle sin recalcularlo —
    espía las 5 funciones caras para probarlo (no cuántas veces build_series
    las invoca, sino cuántas veces se ejecutan de VERDAD)."""
    calls = {"fx_matrix": 0, "nav_series": 0, "currency_buckets": 0,
             "accruals": 0, "flows": 0}
    for name in calls:
        orig = getattr(E, name)
        def make_spy(name, orig):
            def spy(*a, **k):
                calls[name] += 1
                return orig(*a, **k)
            return spy
        monkeypatch.setattr(E, name, make_spy(name, orig))

    inputs = E.precompute_series_inputs(ds, None)
    dates = ds.dates
    mid = len(dates) // 2
    E.attribute(ds, dates[0], dates[mid], precomputed=inputs)
    E.attribute(ds, dates[mid], dates[-1], precomputed=inputs)
    assert all(n == 1 for n in calls.values()), calls   # una sola vez para las DOS ventanas


def test_precompute_series_inputs_matches_individual_calls(ds):
    inputs = E.precompute_series_inputs(ds, None)
    assert inputs.fx_matrix == P.fx_matrix(ds)
    assert inputs.navs == P.nav_series(ds, None)
    assert inputs.buckets == P.currency_buckets(ds, None)
    assert inputs.accr == P.accruals(ds, None)
    assert inputs.flows == P.flows(ds, None)
