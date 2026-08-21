"""
Quant4all — Módulo de benchmark (§19 de la spec).

Descarga series de benchmark desde Yahoo Finance, las reindexa al calendario
del motor y calcula las métricas relativas de §19.4.

Dos entradas independientes a propósito:
  - fetch_benchmark()   necesita red. Descarga y cachea en CSV.
  - load_benchmark()    trabaja sólo sobre la caché. Sin red.

Así el pipeline de cálculo es reproducible y testeable aunque Yahoo
esté caído o cambie de formato.

Dependencias: pandas, numpy, yfinance
    pip install yfinance
"""

from __future__ import annotations

import os
import math
import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

import q4_probe as PR

CACHE_DIR = os.environ.get("Q4_CACHE", "./cache_benchmark")

# --------------------------------------------------------------------------
# Catálogo de benchmarks
# --------------------------------------------------------------------------
# `adjusted=True` significa que usamos el precio ajustado por dividendos, que
# es un proxy de retorno total. Es la vía práctica en Yahoo: los índices de
# retorno total nativos (^SP500TR) existen pero tienen histórico irregular y
# no cubren todos los casos. Los ETF ajustados sí.
#
# REGLA §19.5: siempre al menos dos benchmarks, uno amplio y uno de estilo.

BENCHMARKS = {
    # amplios
    "SPY":     dict(name="S&P 500 (SPY, ajustado)",       currency="USD", kind="broad"),
    "ACWI":    dict(name="MSCI ACWI (ACWI, ajustado)",    currency="USD", kind="broad"),
    "^SP500TR": dict(name="S&P 500 Total Return",         currency="USD", kind="broad"),
    # estilo comparable a una cartera growth/momentum US
    "QQQ":     dict(name="Nasdaq-100 (QQQ, ajustado)",    currency="USD", kind="style"),
    "MTUM":    dict(name="MSCI USA Momentum (MTUM)",      currency="USD", kind="style"),
    "IWF":     dict(name="Russell 1000 Growth (IWF)",     currency="USD", kind="style"),
}

DEFAULT_SET = ["SPY", "QQQ"]


# --------------------------------------------------------------------------
# Descarga y caché
# --------------------------------------------------------------------------

def fetch_benchmark(ticker: str, start: str, end: str, force: bool = False) -> pd.Series:
    """Descarga de Yahoo y cachea. `start`/`end` en formato YYYYMMDD.

    Devuelve una Serie indexada por fecha (str YYYYMMDD) con el precio
    ajustado por dividendos.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{ticker.replace('^','_')}.csv")

    # Se conserva un margen de días ANTES de `start`: si la primera fecha del
    # motor cae en un día sin cotización del benchmark (p. ej. 1-ene, o un
    # festivo US), reindex_to_engine necesita un precio previo que arrastrar
    # (§15.3). Sin ese margen, un periodo que empieza en festivo reventaba.
    lead = (dt.datetime.strptime(start, "%Y%m%d") - dt.timedelta(days=10)).strftime("%Y%m%d")

    if os.path.exists(path) and not force:
        cached = load_benchmark(ticker)
        if cached.index.min() <= start and cached.index.max() >= end:
            return cached.loc[lead:end]

    import yfinance as yf

    d0 = dt.datetime.strptime(start, "%Y%m%d") - dt.timedelta(days=7)
    d1 = dt.datetime.strptime(end, "%Y%m%d") + dt.timedelta(days=2)

    # Sólo esta parte va a red -- separada de la lectura de caché de arriba
    # a propósito, para que la métrica (Fase 0 del plan de escalabilidad)
    # mida el coste real de un miss, sin diluirlo con los hits (casi
    # gratis) en el mismo agregado.
    with PR.timed("fetch_benchmark_network", ticker=ticker):
        df = yf.download(
            ticker,
            start=d0.strftime("%Y-%m-%d"),
            end=d1.strftime("%Y-%m-%d"),
            auto_adjust=True,      # <-- CRÍTICO: retorno total, no índice de precios (§19.1)
            progress=False,
            actions=False,
        )
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo no devolvió datos para {ticker}")

    close = df["Close"]
    if isinstance(close, pd.DataFrame):       # yfinance devuelve MultiIndex si hay 1 ticker
        close = close.iloc[:, 0]

    s = pd.Series(close.to_numpy(dtype=float),
                  index=[d.strftime("%Y%m%d") for d in close.index],
                  name=ticker)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s.to_csv(path, header=True)
    return s.loc[lead:end]


def load_benchmark(ticker: str) -> pd.Series:
    """Lee de caché. No toca la red."""
    path = os.path.join(CACHE_DIR, f"{ticker.replace('^','_')}.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No hay caché para {ticker}: ejecuta fetch_benchmark primero")
    df = pd.read_csv(path, index_col=0)
    s = df.iloc[:, 0]
    s.index = [str(i) for i in s.index]
    s.name = ticker
    return s.sort_index()


# --------------------------------------------------------------------------
# Reindexado al calendario del motor (§19.3)
# --------------------------------------------------------------------------

@dataclass
class Reindexed:
    returns: pd.Series      # retornos diarios alineados a las fechas del motor
    n_forward_filled: int   # días arrastrados
    pct_forward_filled: float
    reliable_vol: bool      # False si el arrastre supera el 3 %


def reindex_to_engine(prices: pd.Series, engine_dates: list[str]) -> Reindexed:
    """Reindexa el benchmark al calendario del motor.

    Las fechas del motor mandan. Los huecos se rellenan arrastrando el último
    precio, lo que genera retornos exactamente 0 que amortiguan la volatilidad
    del benchmark. Por eso se cuentan y se reporta si son demasiados.
    """
    engine_dates = sorted(engine_dates)
    idx = pd.Index(sorted(set(prices.index) | set(engine_dates)))
    p = prices.reindex(idx).ffill().loc[engine_dates]

    if p.isna().any():
        first_ok = p.first_valid_index()
        raise ValueError(
            f"El benchmark no cubre el inicio del período "
            f"(primer dato utilizable: {first_ok}, se pedía desde {engine_dates[0]})"
        )

    filled = sum(1 for d in engine_dates if d not in prices.index)
    r = p.pct_change().dropna()
    pct = 100.0 * filled / len(engine_dates)
    return Reindexed(r, filled, pct, pct <= 3.0)


def to_currency(prices: pd.Series, fx: dict[str, float], dates: list[str]) -> pd.Series:
    """Convierte una serie de precios a la moneda de análisis.

    `fx[d]` = unidades de la moneda de análisis por 1 unidad de la divisa del
    benchmark, en la fecha d. Se aplica el FX del mismo día, nunca uno de cierre.
    Ver §19.2: sólo para comparar contra la serie Total, jamás contra Estrategia.
    """
    return pd.Series({d: prices[d] * fx[d] for d in dates if d in prices.index})


# --------------------------------------------------------------------------
# Métricas relativas (§19.4)
# --------------------------------------------------------------------------

P_ANNUAL = 252


def _cagr(r: np.ndarray, p: int = P_ANNUAL) -> float:
    total = float(np.prod(1.0 + r))
    return total ** (p / len(r)) - 1.0


def relative_metrics(r_p: pd.Series, r_b: pd.Series, rf: float = 0.0,
                     p: int = P_ANNUAL) -> dict:
    """Métricas de la cartera frente al benchmark.

    r_p, r_b: retornos diarios simples, ya alineados a las mismas fechas.
    rf: tasa libre de riesgo ANUAL (§18.3 — no la dejes en 0 sin decirlo).
    """
    common = r_p.index.intersection(r_b.index)
    a = r_p.loc[common].to_numpy(dtype=float)
    b = r_b.loc[common].to_numpy(dtype=float)
    n = len(a)
    if n < 20:
        raise ValueError(f"Muestra insuficiente: {n} observaciones")

    var_b = float(np.var(b, ddof=1))
    beta = float(np.cov(a, b, ddof=1)[0, 1] / var_b) if var_b > 0 else float("nan")

    cagr_p, cagr_b = _cagr(a, p), _cagr(b, p)
    alpha = cagr_p - (rf + beta * (cagr_b - rf))

    diff = a - b
    te = float(np.std(diff, ddof=1)) * math.sqrt(p)
    ir = (cagr_p - cagr_b) / te if te > 0 else float("nan")

    up, dn = b > 0, b < 0
    up_cap = float(a[up].mean() / b[up].mean()) if up.sum() else float("nan")
    dn_cap = float(a[dn].mean() / b[dn].mean()) if dn.sum() else float("nan")

    annualizable = n >= p       # §18.3: por debajo de un año no se anualiza

    return dict(
        n=n,
        cagr_portfolio=cagr_p * 100 if annualizable else float("nan"),
        cagr_benchmark=cagr_b * 100 if annualizable else float("nan"),
        cum_portfolio=(float(np.prod(1 + a)) - 1) * 100,
        cum_benchmark=(float(np.prod(1 + b)) - 1) * 100,
        beta=beta,
        alpha=alpha * 100 if annualizable else float("nan"),
        tracking_error=te * 100,
        information_ratio=ir if annualizable else float("nan"),
        up_capture=up_cap * 100,
        down_capture=dn_cap * 100,
        correlation=float(np.corrcoef(a, b)[0, 1]),
        annualizable=annualizable,
    )


# --------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------

def compare(engine_dates: list[str],
            r_total: pd.Series,
            r_strategy: pd.Series,
            fx_to_analysis: dict[str, float] | None = None,
            tickers: list[str] | None = None,
            rf: float = 0.0,
            offline: bool = False) -> pd.DataFrame:
    """Compara ambas series contra cada benchmark respetando §19.2.

    - r_strategy (FX-neutral)  ->  benchmark en SU divisa local
    - r_total (moneda análisis) ->  benchmark convertido con FX diario

    fx_to_analysis[d] = unidades de la moneda de análisis por 1 USD en la
    fecha d. Si la moneda de análisis es USD, pasa None o un dict de unos.
    """
    tickers = tickers or DEFAULT_SET
    rows = []

    for tk in tickers:
        prices = load_benchmark(tk) if offline else fetch_benchmark(
            tk, engine_dates[0], engine_dates[-1])

        # --- Estrategia vs benchmark local ---
        rx = reindex_to_engine(prices, engine_dates)
        m = relative_metrics(r_strategy, rx.returns, rf=rf)
        m.update(ticker=tk, name=BENCHMARKS.get(tk, {}).get("name", tk),
                 serie="Estrategia", ff_pct=rx.pct_forward_filled,
                 vol_fiable=rx.reliable_vol)
        rows.append(m)

        # --- Total vs benchmark convertido ---
        if fx_to_analysis:
            conv = to_currency(prices, fx_to_analysis,
                               [d for d in engine_dates if d in fx_to_analysis])
            rx2 = reindex_to_engine(conv, engine_dates)
        else:
            rx2 = rx
        m2 = relative_metrics(r_total, rx2.returns, rf=rf)
        m2.update(ticker=tk, name=BENCHMARKS.get(tk, {}).get("name", tk),
                  serie="Total", ff_pct=rx2.pct_forward_filled,
                  vol_fiable=rx2.reliable_vol)
        rows.append(m2)

    cols = ["ticker", "serie", "cum_portfolio", "cum_benchmark", "cagr_portfolio",
            "cagr_benchmark", "beta", "alpha", "tracking_error",
            "information_ratio", "up_capture", "down_capture", "correlation",
            "ff_pct", "vol_fiable", "n"]
    return pd.DataFrame(rows)[cols]
