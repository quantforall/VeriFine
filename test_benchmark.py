"""Tests del módulo de benchmark (§15) — offline, sin tocar Yahoo."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import q4_benchmark as B


def _engine_dates(n=300):
    base = pd.Timestamp("2023-01-02")
    return [(base + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def test_reindex_counts_forward_filled():
    dates = _engine_dates(200)
    # el benchmark no tiene 1 de cada 10 fechas del motor -> se arrastran
    bench_dates = [d for i, d in enumerate(dates) if i % 10 != 3]
    px = pd.Series(100 * np.cumprod(1 + np.full(len(bench_dates), 0.001)), index=bench_dates)
    rx = B.reindex_to_engine(px, dates)
    assert rx.n_forward_filled == sum(1 for d in dates if d not in bench_dates)
    assert rx.pct_forward_filled == pytest.approx(100 * rx.n_forward_filled / len(dates))
    # ~10 % arrastrado supera el 3 % -> vol no fiable (§15.3)
    assert rx.reliable_vol is False


def test_reindex_raises_if_benchmark_starts_late():
    dates = _engine_dates(50)
    px = pd.Series([100.0, 101.0], index=dates[10:12])   # empieza tarde
    with pytest.raises(ValueError):
        B.reindex_to_engine(px, dates)


def test_relative_metrics_proportional_series():
    """Si r_p = 2·r_b, beta=2, correlación=1 y capturas=2 (identidades exactas)."""
    rng = np.random.default_rng(0)
    idx = _engine_dates(300)[1:]
    r_b = pd.Series(rng.normal(0.0004, 0.01, len(idx)), index=idx)
    r_p = 2.0 * r_b
    m = B.relative_metrics(r_p, r_b, rf=0.0)
    assert m["beta"] == pytest.approx(2.0, abs=1e-9)
    assert m["correlation"] == pytest.approx(1.0, abs=1e-9)
    assert m["up_capture"] == pytest.approx(200.0, abs=1e-6)
    assert m["down_capture"] == pytest.approx(200.0, abs=1e-6)


def test_relative_metrics_needs_min_sample():
    idx = _engine_dates(15)[1:]
    r = pd.Series(np.zeros(len(idx)), index=idx)
    with pytest.raises(ValueError):
        B.relative_metrics(r, r, rf=0.0)


def test_catalog_has_sp500_total_return():
    assert "^SP500TR" in B.BENCHMARKS
    assert B.BENCHMARKS["^SP500TR"]["currency"] == "USD"


def test_fetch_keeps_lead_before_start(tmp_path, monkeypatch):
    """fetch_benchmark conserva margen previo a `start` para poder arrastrar la
    primera fecha del motor si cae en festivo del benchmark (§15.3)."""
    monkeypatch.setattr(B, "CACHE_DIR", str(tmp_path))
    idx = ["20231228", "20231229", "20240102", "20240103", "20240104"]
    s = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0], index=idx, name="^TEST")
    s.to_csv(tmp_path / "_TEST.csv", header=True)
    # el periodo empieza el 1-ene (sin cotización); la caché lo cubre
    out = B.fetch_benchmark("^TEST", "20240101", "20240104")
    assert out.index.min() < "20240101"          # hay dato previo para arrastrar
    # y con ese margen, reindexar a un motor que incluye el 1-ene no falla
    rx = B.reindex_to_engine(out, ["20240101", "20240102", "20240103", "20240104"])
    assert len(rx.returns) == 3
