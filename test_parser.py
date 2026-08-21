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


def test_load_merges_files_with_different_movement_schemas(tmp_path):
    """Regresión: parse_file_cached() cachea cada tabla en formato COLUMNAR
    (una lista por campo, no una lista de dicts), y `movements` mezcla
    CashTransaction y Transfer — dos esquemas de columnas DISTINTOS (ver
    parse_file()). Fundir dos ficheros que traigan uno de cada tipo debe
    alinear las columnas (pd.concat), no desalinearlas ni reventar con
    'arrays must be of the same length'."""
    xml1 = (
        '<FlexQueryResponse><FlexStatements>'
        '<FlexStatement accountId="A1" fromDate="20260101" toDate="20260101">'
        '<EquitySummaryInBase><EquitySummaryByReportDateInBase accountId="A1"'
        ' reportDate="20260101" total="800" cash="300" stock="500"/></EquitySummaryInBase>'
        '<CashTransactions><CashTransaction accountId="A1" transactionID="TX1"'
        ' reportDate="20260101" settleDate="20260101" type="Dividends" currency="USD"'
        ' symbol="XX" conid="1" amount="5" fxRateToBase="1.0"/></CashTransactions>'
        '</FlexStatement></FlexStatements></FlexQueryResponse>')
    xml2 = (
        '<FlexQueryResponse><FlexStatements>'
        '<FlexStatement accountId="A1" fromDate="20260102" toDate="20260102">'
        '<EquitySummaryInBase><EquitySummaryByReportDateInBase accountId="A1"'
        ' reportDate="20260102" total="820" cash="320" stock="500"/></EquitySummaryInBase>'
        '<Transfers><Transfer accountId="A1" transactionID="TR1" reportDate="20260102"'
        ' settleDate="20260102" assetCategory="STK" symbol="XX" conid="1" quantity="1"'
        ' cashTransfer="0" positionAmount="100" positionAmountInBase="100" currency="USD"'
        ' fxRateToBase="1.0" account="A2"/></Transfers>'
        '</FlexStatement></FlexStatements></FlexQueryResponse>')
    p1 = tmp_path / "f1.xml"
    p2 = tmp_path / "f2.xml"
    p1.write_text(xml1)
    p2.write_text(xml2)

    ds = P.load([str(p1), str(p2)])
    assert set(ds.movements["movement_id"]) == {"TX1", "TR1"}
    tx = ds.movements[ds.movements["movement_id"] == "TX1"].iloc[0]
    tr = ds.movements[ds.movements["movement_id"] == "TR1"].iloc[0]
    assert tx["type"] == "Dividends"
    assert tr["type"] == "TRANSFER_STK"
    assert tr["quantity"] == pytest.approx(1.0)   # sólo Transfer tiene "quantity"
    assert pd.isna(tx["quantity"])                # CashTransaction no la tiene -> NaN, no desalineado


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


def test_merge_columnar_aligns_mismatched_schemas():
    """Dos 'ficheros' con columnas distintas para la misma tabla (como
    movements: CashTransaction vs Transfer) deben fundirse igual que
    pd.DataFrame() haría con la lista de filas ya mezclada — rellenando con
    None donde a un fichero le falte una columna que el otro sí trae."""
    chunk1 = {"a": [1, 2], "b": [10, 20]}          # 2 filas, columnas a,b
    chunk2 = {"a": [3], "c": [30]}                  # 1 fila, columnas a,c (sin b)
    merged = P._merge_columnar([chunk1, chunk2])
    assert merged == {"a": [1, 2, 3], "b": [10, 20, None], "c": [None, None, 30]}
    assert pd.DataFrame(merged).equals(
        pd.DataFrame([{"a": 1, "b": 10}, {"a": 2, "b": 20}, {"a": 3, "c": 30}]))


def test_merge_columnar_empty_and_single_chunk():
    assert P._merge_columnar([]) == {}
    assert P._merge_columnar([{}]) == {}
    assert P._merge_columnar([{"a": [1, 2]}]) == {"a": [1, 2]}


def test_parse_file_cached_writes_sidecar_and_reuses_it(tmp_path):
    """La primera llamada parsea y escribe `<path>.parsed.json`; si el XML
    cambia después (no debería pasar nunca en producción — el crudo es
    inmutable, ver q4_ingest.py — pero así se prueba que de verdad se lee
    la caché y no se vuelve a tocar el XML), la caché sigue mandando.

    El resultado va en formato COLUMNAR (`{"total": [800.0]}`, no
    `[{"total": 800.0}]`) — ver `_rows_to_columnar()`."""
    p = tmp_path / "mini.xml"
    p.write_text(_mini_xml(total=800, cash=300, stock=500, options=0,
                          pos_cat="STK", pos_ccy="USD", pos_value=500))
    cache_path = tmp_path / "mini.xml.parsed.json"

    first = P.parse_file_cached(str(p))
    assert cache_path.exists()
    assert first["nav"]["total"][0] == pytest.approx(800.0)

    p.write_text(_mini_xml(total=999, cash=300, stock=500, options=0,
                          pos_cat="STK", pos_ccy="USD", pos_value=500))
    second = P.parse_file_cached(str(p))
    assert second["nav"]["total"][0] == pytest.approx(800.0)   # de la caché, no del XML nuevo


def test_parse_file_cached_reparses_on_corrupt_cache(tmp_path):
    p = tmp_path / "mini.xml"
    p.write_text(_mini_xml(total=800, cash=300, stock=500, options=0,
                          pos_cat="STK", pos_ccy="USD", pos_value=500))
    cache_path = tmp_path / "mini.xml.parsed.json"
    cache_path.write_text("{ esto no es json valido")

    result = P.parse_file_cached(str(p))
    assert result["nav"]["total"][0] == pytest.approx(800.0)   # reparseado del XML
    assert json.loads(cache_path.read_text())["nav"]["total"][0] == pytest.approx(800.0)  # regenerada


def test_parse_file_cached_migrates_old_row_oriented_cache(tmp_path):
    """Cachés escritas ANTES de este cambio (fila-orientada: una lista de
    dicts, no columnar) deben seguir sirviendo — se convierten solas, en
    memoria, sin volver a tocar el XML, y el fichero en disco queda ya
    reescrito en columnar para la próxima vez."""
    p = tmp_path / "mini.xml"
    p.write_text(_mini_xml(total=800, cash=300, stock=500, options=0,
                          pos_cat="STK", pos_ccy="USD", pos_value=500))
    cache_path = tmp_path / "mini.xml.parsed.json"
    old_format = dict(
        nav=[dict(account="A1", date="20260102", total=800.0, accruals=0.0,
                  cash_base=300.0, currency="EUR")],
        positions=[], cash=[], fx=[], movements=[], boot=[], trades=[],
        corporate_actions=[], base_currency="EUR")
    cache_path.write_text(json.dumps(old_format))

    result = P.parse_file_cached(str(p))
    assert result["nav"]["total"][0] == pytest.approx(800.0)   # migrado, no reparseado (el XML dice 800 igualmente)
    assert result["nav"]["account"][0] == "A1"                 # dato real de la caché VIEJA, no del XML

    rewritten = json.loads(cache_path.read_text())
    assert isinstance(rewritten["nav"], dict)                  # ya reescrita en columnar
    assert rewritten["nav"]["total"][0] == pytest.approx(800.0)


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
