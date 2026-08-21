"""Tests de q4_trades.py (§20/§21 — Operaciones y Cartera) sobre datasets
construidos a mano.

No existían tests de este módulo antes de esto — se escriben ahora como
caracterización del comportamiento ACTUAL (verificado a mano, ver los
valores en cada aserción) antes de convertir sus `.iterrows()` a
`.itertuples()` por rendimiento (§ repaso de rendimiento): la garantía real
de que ese cambio no altera ni un número es que estos tests sigan en verde
exactamente igual, antes y después."""

from __future__ import annotations

import pandas as pd
import pytest

import q4_parser as P
import q4_trades as T


# --------------------------------------------------------------------------
# build() — §20: FIFO, splits, transfers, no_determinado, dividendos
# --------------------------------------------------------------------------

@pytest.fixture
def trades_ds() -> P.Dataset:
    """Cuenta A1, cuatro símbolos que ejercitan cada rama de _fifo_operations():

    AAA  compra 10@100 (01-01) -> split 2x1 (01-15, qty 20@50) -> vende
         8@60 (02-01, cierre parcial): 12 quedan abiertas. Dividendo +15.
    BBB  vende 5@20 SIN compra previa ni Transfer -> no_determinado.
    CCC  vende 5@120 con un Transfer previo (05-01, 5@100) que explica el
         origen -> entry_source="transfer".
    """
    trades = pd.DataFrame([
        dict(account="A1", trade_id="T1", conid="1", symbol="AAA", underlying_symbol=None,
            asset_category="STK", put_call=None, strike=None, expiry=None, multiplier=1.0,
            currency="EUR", date="20240101", datetime="20240101;100000", quantity=10,
            trade_price=100.0, commission_local=-1.0, open_close="O", buy_sell="BUY",
            fifo_pnl_realized_local=0.0),
        dict(account="A1", trade_id="T2", conid="1", symbol="AAA", underlying_symbol=None,
            asset_category="STK", put_call=None, strike=None, expiry=None, multiplier=1.0,
            currency="EUR", date="20240201", datetime="20240201;100000", quantity=-8,
            trade_price=60.0, commission_local=-1.0, open_close="C", buy_sell="SELL",
            fifo_pnl_realized_local=80.0),
        dict(account="A1", trade_id="T3", conid="2", symbol="BBB", underlying_symbol=None,
            asset_category="STK", put_call=None, strike=None, expiry=None, multiplier=1.0,
            currency="EUR", date="20240110", datetime="20240110;100000", quantity=-5,
            trade_price=20.0, commission_local=-0.5, open_close="C", buy_sell="SELL",
            fifo_pnl_realized_local=25.0),
        dict(account="A1", trade_id="T4", conid="3", symbol="CCC", underlying_symbol=None,
            asset_category="STK", put_call=None, strike=None, expiry=None, multiplier=1.0,
            currency="EUR", date="20240120", datetime="20240120;100000", quantity=-5,
            trade_price=120.0, commission_local=-0.5, open_close="C", buy_sell="SELL",
            fifo_pnl_realized_local=100.0),
    ])
    ca = pd.DataFrame([dict(account="A1", action_id="CA1", conid="1", symbol="AAA",
                           datetime="20240115;000000", ratio_num=2, ratio_den=1)])
    mov = pd.DataFrame([
        dict(account="A1", movement_id="TR1", date="20240105", settle_date="20240105",
            type="TRANSFER_STK", symbol="CCC", conid="3", quantity=5,
            position_amount_local=500.0, position_amount_base=500.0, currency="EUR",
            amount_local=0.0, fx_to_base=1.0, flow_base=0.0, is_flow=True,
            counterparty="OTHER", source="transfer"),
        dict(account="A1", movement_id="DIV1", date="20240210", settle_date="20240210",
            type="Dividends", currency="EUR", symbol="AAA", conid="1", amount_local=15.0,
            fx_to_base=1.0, flow_base=0.0, is_flow=False, counterparty=None, source="cash"),
    ])
    return P.Dataset(trades=trades, corporate_actions=ca, movements=mov, base_currency="EUR")


def test_build_empty_trades_returns_empty():
    assert T.build(P.Dataset(), "20240101", "20241231") == ([], [])


def test_build_split_adjusts_lot_before_matching(trades_ds):
    """AAA: split 2x1 entre la compra y la venta -> el cierre debe casar
    contra el lote YA ajustado (20@50), no contra el original (10@100)."""
    detail, _ = T.build(trades_ds, "20240101", "20241231")
    aaa = [o for o in detail if o.symbol == "AAA"]
    closed = next(o for o in aaa if o.status == "cerrada")
    open_ = next(o for o in aaa if o.status == "abierta")

    assert closed.entry_price == pytest.approx(50.0)      # 100 / 2 (split)
    assert closed.quantity == pytest.approx(8.0)
    assert closed.gain_local == pytest.approx(80.0)        # fifo_pnl_realized_local completo (frac=1.0)
    assert closed.entry_commission_local == pytest.approx(-0.4)   # -1.0 * (8/20)
    assert closed.pct_return == pytest.approx(0.2)         # (60-50)/50

    assert open_.quantity == pytest.approx(12.0)           # 20 - 8
    assert open_.entry_price == pytest.approx(50.0)
    assert open_.entry_commission_local == pytest.approx(-1.0)   # comisión TOTAL del lote, sin prorratear


def test_build_no_lot_or_transfer_is_no_determinado(trades_ds):
    """BBB: cierre sin compra previa ni Transfer -> no_determinado, sin
    precio/fecha de entrada, pero el importe (fifoPnlRealized) se conserva."""
    detail, _ = T.build(trades_ds, "20240101", "20241231")
    bbb = next(o for o in detail if o.symbol == "BBB")
    assert bbb.entry_source == "no_determinado"
    assert bbb.entry_price is None
    assert bbb.entry_date is None
    assert bbb.gain_local == pytest.approx(25.0)
    assert bbb.pct_return is None                          # sin precio de entrada, no hay % que calcular


def test_build_transfer_explains_origin(trades_ds):
    """CCC: cierre con un Transfer previo que sí explica el origen."""
    detail, _ = T.build(trades_ds, "20240101", "20241231")
    ccc = next(o for o in detail if o.symbol == "CCC")
    assert ccc.entry_source == "transfer"
    assert ccc.entry_price == pytest.approx(100.0)          # 500 / 5 (position_amount_base / qty)
    assert ccc.entry_date == "20240105"
    assert ccc.gain_local == pytest.approx(100.0)


def test_build_dividend_attributed_to_ticker(trades_ds):
    _, aggs = T.build(trades_ds, "20240101", "20241231")
    by_symbol = {a.symbol: a for a in aggs}
    assert by_symbol["AAA"].dividendos_local == pytest.approx(15.0)
    assert by_symbol["AAA"].revalorizacion_local == pytest.approx(80.0)
    assert by_symbol["AAA"].comisiones_local == pytest.approx(-2.4)   # -0.4 (cerrada) + -1.0 (abierta) + -1.0 (exit)
    assert by_symbol["AAA"].total_local == pytest.approx(92.6)
    assert by_symbol["BBB"].dividendos_local == pytest.approx(0.0)
    assert by_symbol["CCC"].total_local == pytest.approx(99.5)        # 100.0 - 0.5 comisión


def test_build_filters_by_account_and_period(trades_ds):
    # BBB (cierra 01-10) y CCC (cierra 01-20) ya no solapan con esta
    # ventana; AAA sigue con actividad (cierre parcial 02-01 + lote abierto).
    detail, _ = T.build(trades_ds, "20240201", "20240301")
    symbols = {o.symbol for o in detail}
    assert symbols == {"AAA"}

    detail_a2, _ = T.build(trades_ds, "20240101", "20241231", accounts=["A2"])
    assert detail_a2 == []


# --------------------------------------------------------------------------
# portfolio() — §21: posiciones, respaldo de cost_basis_price, exclusión de
# futuros/opciones del patrimonio, efectivo, pastel.
# --------------------------------------------------------------------------

@pytest.fixture
def portfolio_ds() -> P.Dataset:
    """AAA con cost_basis_price real; DDD sin cost_basis_price ni FIFO que lo
    respalde (queda en None, nunca 0.0 disfrazado); FUT1 fuera del
    patrimonio/pastel pero dentro de la exposición."""
    nav = pd.DataFrame([
        dict(account="A1", date="20240101", total=1000.0, accruals=0.0, cash_base=100.0, currency="EUR"),
        dict(account="A1", date="20240301", total=1500.0, accruals=0.0, cash_base=300.0, currency="EUR"),
    ])
    trades = pd.DataFrame([
        dict(account="A1", trade_id="T1", conid="1", symbol="AAA", underlying_symbol=None,
            asset_category="STK", put_call=None, strike=None, expiry=None, multiplier=1.0,
            currency="EUR", date="20240101", datetime="20240101;100000", quantity=10,
            trade_price=100.0, commission_local=-1.0, open_close="O", buy_sell="BUY",
            fifo_pnl_realized_local=0.0),
    ])
    positions = pd.DataFrame([
        dict(account="A1", date="20240301", conid="1", symbol="AAA", asset_category="STK",
            currency="EUR", quantity=10.0, price=120.0, value_local=1200.0,
            description="AAA CORP", cost_basis_price=100.0, cost_basis_money=1000.0,
            fifo_pnl_unrealized=200.0),
        dict(account="A1", date="20240301", conid="4", symbol="DDD", asset_category="STK",
            currency="EUR", quantity=5.0, price=50.0, value_local=250.0,
            description="DDD INC", cost_basis_price=None, cost_basis_money=None,
            fifo_pnl_unrealized=None),
        dict(account="A1", date="20240301", conid="5", symbol="FUT1", asset_category="FUT",
            currency="USD", quantity=-2.0, price=100.0, value_local=-20000.0,
            description="FUT CONTRACT", cost_basis_price=None, cost_basis_money=None,
            fifo_pnl_unrealized=500.0),
    ])
    cash = pd.DataFrame([
        dict(account="A1", date="20240301", currency="EUR", balance_local=300.0),
        dict(account="A1", date="20240301", currency="USD", balance_local=100.0),
    ])
    fx = pd.DataFrame([dict(date="20240101", currency="USD", rate=0.9),
                       dict(date="20240301", currency="USD", rate=0.9)])
    return P.Dataset(nav=nav, trades=trades, positions=positions, cash=cash, fx=fx,
                     base_currency="EUR")


def test_portfolio_empty_positions_returns_empty_snapshot():
    snap = T.portfolio(P.Dataset(positions=pd.DataFrame()))
    assert snap.positions == [] and snap.cash == [] and snap.pie == []
    assert snap.equity_total_analysis_ccy == 0.0


def test_portfolio_uses_real_cost_basis_price(portfolio_ds):
    snap = T.portfolio(portfolio_ds, weight_basis="patrimonio")
    aaa = next(r for r in snap.positions if r.symbol == "AAA")
    assert aaa.entry_price == pytest.approx(100.0)
    assert aaa.unrealized_gain_local == pytest.approx(200.0)   # de fifo_pnl_unrealized de IBKR
    assert aaa.in_equity_weight is True
    assert aaa.description == "Aaa Corp"                        # .title() sobre "AAA CORP"


def test_portfolio_missing_cost_basis_stays_none_without_fifo_backup(portfolio_ds):
    """DDD no tiene cost_basis_price de IBKR NI ningún Trade que lo respalde
    (no hay Trade de DDD en el fixture) -> debe quedar en None, nunca en 0.0."""
    snap = T.portfolio(portfolio_ds, weight_basis="patrimonio")
    ddd = next(r for r in snap.positions if r.symbol == "DDD")
    assert ddd.entry_price is None
    assert ddd.unrealized_gain_local is None
    assert ddd.pct_return is None


def test_portfolio_excludes_futures_from_equity_but_counts_exposure(portfolio_ds):
    snap = T.portfolio(portfolio_ds, weight_basis="patrimonio")
    fut = next(r for r in snap.positions if r.symbol == "FUT1")
    assert fut.in_equity_weight is False
    assert fut.kind == "futuros"
    # equity_total: AAA (1200) + DDD (250) + efectivo (300 + 100*0.9=90) = 1840, SIN el futuro
    assert snap.equity_total_analysis_ccy == pytest.approx(1840.0)
    # exposure_total: equity + |nocional del futuro| (20000*0.9=18000) = 19840
    assert snap.exposure_total_analysis_ccy == pytest.approx(19840.0)
    assert "FUT1" not in {p.label for p in snap.pie}   # excluido del pastel en modo "patrimonio"


def test_portfolio_cash_converted_and_pooled_by_single_slice(portfolio_ds):
    snap = T.portfolio(portfolio_ds, weight_basis="patrimonio")
    by_ccy = {c.currency: c for c in snap.cash}
    assert by_ccy["EUR"].value_analysis_ccy == pytest.approx(300.0)
    assert by_ccy["USD"].value_analysis_ccy == pytest.approx(90.0)   # 100 * 0.9
    efectivo = next(p for p in snap.pie if p.label == "Efectivo")
    assert efectivo.value_analysis_ccy == pytest.approx(390.0)        # 300 + 90, una sola porción


def test_portfolio_pct_weight_uses_equity_total_in_patrimonio_mode(portfolio_ds):
    snap = T.portfolio(portfolio_ds, weight_basis="patrimonio")
    aaa = next(r for r in snap.positions if r.symbol == "AAA")
    assert aaa.pct_weight == pytest.approx(100 * 1200.0 / 1840.0)


def test_portfolio_exposicion_mode_includes_futures_in_pie(portfolio_ds):
    snap = T.portfolio(portfolio_ds, weight_basis="exposicion")
    fut_slice = next((p for p in snap.pie if p.label == "FUT1"), None)
    assert fut_slice is not None
    assert fut_slice.value_analysis_ccy == pytest.approx(18000.0)   # valor absoluto, corto
    aaa = next(r for r in snap.positions if r.symbol == "AAA")
    # denominador ahora es exposure_total (19840), no equity_total
    assert aaa.pct_weight == pytest.approx(100 * 1200.0 / 19840.0)
