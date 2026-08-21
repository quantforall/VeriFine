"""Tests unitarios del parser sobre datasets construidos a mano (sin datos reales)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

import q4_parser as P


def test_currency_buckets_excludes_futures_notional():
    """El nocional de un futuro (FUT) NO entra en los cubos: no es NAV.

    Regresión del bug de 2021: la cuenta llevó un corto de futuros S&P cuyo
    positionValue es el nocional (~-93k). Sumarlo descuadraba la reconstrucción
    del NAV en ~83k €. Su P&L se liquida en efectivo, que sí entra por su cubo.
    """
    ds = P.Dataset(
        positions=pd.DataFrame([
            dict(account="U1", date="20210102", conid="1", symbol="AAA",
                 asset_category="STK", currency="USD",
                 quantity=10, price=100.0, value_local=1000.0),
            dict(account="U1", date="20210102", conid="2", symbol="MESZ1",
                 asset_category="FUT", currency="USD",
                 quantity=-4, price=4679.0, value_local=-93595.0),
        ]),
        cash=pd.DataFrame([
            dict(account="U1", date="20210102", currency="USD", balance_local=500.0),
        ]),
        base_currency="EUR",
    )
    b = P.currency_buckets(ds)
    # sólo la acción (1000) + el efectivo (500); el nocional del futuro se excluye
    assert b["20210102"]["USD"] == pytest.approx(1500.0)


def test_base_currency_cash_reconciled(tmp_path):
    """IBKR no emite FxPosition para la moneda base: una cuenta con efectivo en
    EUR (sin FxPosition) no entra por los cubos. El parser lo reconcilia desde el
    campo `cash` del NAV. Sin el fix, el cubo EUR faltaría y no reconstruiría."""
    xml = (
        '<FlexQueryResponse><FlexStatements>'
        '<FlexStatement accountId="A1" fromDate="20260102" toDate="20260102">'
        '<EquitySummaryInBase>'
        '<EquitySummaryByReportDateInBase accountId="A1" reportDate="20260102"'
        ' total="800" cash="300" stock="500"/></EquitySummaryInBase>'
        '<OpenPositions><OpenPosition accountId="A1" reportDate="20260102" conid="1"'
        ' symbol="XX" assetCategory="STK" currency="USD" position="5" markPrice="100"'
        ' positionValue="500"/></OpenPositions>'
        '<ConversionRates><ConversionRate reportDate="20260102" fromCurrency="USD"'
        ' toCurrency="EUR" rate="1.0"/></ConversionRates>'
        '</FlexStatement></FlexStatements></FlexQueryResponse>')
    p = tmp_path / "mini.xml"
    p.write_text(xml)
    ds = P.load([str(p)])
    buck = P.currency_buckets(ds)
    assert buck["20260102"]["EUR"] == pytest.approx(300.0, abs=0.01)   # reconciliado
    assert buck["20260102"]["USD"] == pytest.approx(500.0, abs=0.01)   # posición real


def _mini_xml(total, cash, stock, options, pos_cat, pos_ccy, pos_value,
              fx_eur_cash=None, date="20260102"):
    """XML mínimo de una cuenta/día para tests de reconstrucción."""
    fxp = (f'<FxPosition accountId="A1" reportDate="{date}" levelOfDetail="SUMMARY"'
           f' fxCurrency="EUR" quantity="{fx_eur_cash}"/>') if fx_eur_cash is not None else ""
    return (
        '<FlexQueryResponse><FlexStatements>'
        f'<FlexStatement accountId="A1" fromDate="{date}" toDate="{date}">'
        '<EquitySummaryInBase>'
        f'<EquitySummaryByReportDateInBase accountId="A1" reportDate="{date}"'
        f' total="{total}" cash="{cash}" stock="{stock}" options="{options}"/>'
        '</EquitySummaryInBase>'
        f'<OpenPositions><OpenPosition accountId="A1" reportDate="{date}" conid="1"'
        f' symbol="XX" assetCategory="{pos_cat}" currency="{pos_ccy}" position="1"'
        f' markPrice="1" positionValue="{pos_value}"/></OpenPositions>'
        f'<FxPositions>{fxp}</FxPositions>'
        f'<ConversionRates><ConversionRate reportDate="{date}" fromCurrency="USD"'
        ' toCurrency="EUR" rate="1.0"/></ConversionRates>'
        '</FlexStatement></FlexStatements></FlexQueryResponse>')


def test_options_not_double_counted(tmp_path):
    """Una opción (OPT) entra por OpenPosition; si accruals no restara `options`,
    se contaría dos veces (posición + accrual) y el NAV no reconstruiría."""
    p = tmp_path / "opt.xml"
    p.write_text(_mini_xml(total=800, cash=300, stock=0, options=500,
                           pos_cat="OPT", pos_ccy="USD", pos_value=500, fx_eur_cash=300))
    ds = P.load([str(p)])
    assert P.accruals(ds)["20260102"] == pytest.approx(0.0, abs=0.01)   # no absorbe la opción
    buck = P.currency_buckets(ds)
    assert buck["20260102"]["USD"] == pytest.approx(500.0, abs=0.01)     # la opción sí, como posición
    assert buck["20260102"]["EUR"] == pytest.approx(300.0, abs=0.01)


def test_internal_transfer_nets_on_consolidation():
    """T7/T8: una transferencia interna tiene dos patas (mismo transactionID en
    cuentas distintas). Al consolidar ambas cuentas se anula; analizando una
    cuenta sola, cuenta como flujo externo. Requiere que el dedup conserve las
    dos patas (subset=[movement_id, account])."""
    mov = pd.DataFrame([
        dict(account="A", movement_id="TX1", date="20260301", type="TRANSFER_CASH",
             currency="EUR", amount_local=-3000.0, fx_to_base=1.0, flow_base=-3000.0,
             is_flow=True, counterparty="B", source="transfer"),
        dict(account="B", movement_id="TX1", date="20260301", type="TRANSFER_CASH",
             currency="EUR", amount_local=3000.0, fx_to_base=1.0, flow_base=3000.0,
             is_flow=True, counterparty="A", source="transfer"),
    ])
    ds = P.Dataset(movements=mov, base_currency="EUR")
    assert P.flows(ds, ["A", "B"]) == {}                        # T7: interna, se anula
    assert P.flows(ds, ["A"]) == {"20260301": [("EUR", -3000.0)]}  # T8: salida externa
    assert P.flows(ds, ["B"]) == {"20260301": [("EUR", 3000.0)]}   # entrada externa


def test_parse_file_cached_writes_sidecar_and_reuses_it(tmp_path):
    """La primera llamada parsea y escribe `<path>.parsed.json`; si el XML
    cambia después (no debería pasar nunca en producción — el crudo es
    inmutable, ver q4_ingest.py — pero así se prueba que de verdad se lee
    la caché y no se vuelve a tocar el XML), la caché sigue mandando."""
    p = tmp_path / "mini.xml"
    p.write_text(_mini_xml(total=800, cash=300, stock=500, options=0,
                          pos_cat="STK", pos_ccy="USD", pos_value=500))
    cache_path = tmp_path / "mini.xml.parsed.json"

    first = P.parse_file_cached(str(p))
    assert cache_path.exists()
    assert first["nav"][0]["total"] == pytest.approx(800.0)

    p.write_text(_mini_xml(total=999, cash=300, stock=500, options=0,
                          pos_cat="STK", pos_ccy="USD", pos_value=500))
    second = P.parse_file_cached(str(p))
    assert second["nav"][0]["total"] == pytest.approx(800.0)   # de la caché, no del XML nuevo


def test_parse_file_cached_reparses_on_corrupt_cache(tmp_path):
    p = tmp_path / "mini.xml"
    p.write_text(_mini_xml(total=800, cash=300, stock=500, options=0,
                          pos_cat="STK", pos_ccy="USD", pos_value=500))
    cache_path = tmp_path / "mini.xml.parsed.json"
    cache_path.write_text("{ esto no es json valido")

    result = P.parse_file_cached(str(p))
    assert result["nav"][0]["total"] == pytest.approx(800.0)   # reparseado del XML
    assert json.loads(cache_path.read_text())["nav"][0]["total"] == pytest.approx(800.0)  # regenerada


def test_currency_buckets_keeps_stocks():
    """Sin FUT, los cubos suman posiciones + efectivo por divisa."""
    ds = P.Dataset(
        positions=pd.DataFrame([
            dict(account="U1", date="20210102", conid="1", symbol="AAA",
                 asset_category="STK", currency="EUR",
                 quantity=10, price=100.0, value_local=1000.0),
            dict(account="U1", date="20210102", conid="2", symbol="BBB",
                 asset_category="STK", currency="USD",
                 quantity=5, price=200.0, value_local=1000.0),
        ]),
        cash=pd.DataFrame([
            dict(account="U1", date="20210102", currency="EUR", balance_local=250.0),
        ]),
        base_currency="EUR",
    )
    b = P.currency_buckets(ds)
    assert b["20210102"]["EUR"] == pytest.approx(1250.0)
    assert b["20210102"]["USD"] == pytest.approx(1000.0)
