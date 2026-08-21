"""Tests de las claves de caché de proceso en app.py (§B/§ambicioso del
repaso de rendimiento: la caché de `st.cache_data` debe sobrevivir entre
sesiones — recarga de pestaña, otra pestaña, contenedor reciclado — para el
MISMO histórico, no solo dentro de una sesión; y `_cached_series_inputs()`
debe compartirse entre periodos distintos del mismo histórico/cuentas).

Importa `app` en "modo bare" (sin un ScriptRunContext de Streamlit real) —
funciona porque nada a nivel de módulo depende de una sesión activa; sólo
usamos aquí funciones puras (`load_paths`, `_stable_cache_key`, ...).

IMPORTANTE: cada test usa un nombre de fichero ÚNICO (nunca "a.xml" a secas
en más de un test). El motivo no es estético: `load_paths()`/
`_cached_series_inputs()` cachean por NOMBRE, a propósito, asumiendo que un
mismo nombre es SIEMPRE el mismo contenido (verdad en producción, porque el
crudo es inmutable — ver q4_parser.parse_file_cached). Dentro de un mismo
proceso de pytest esa caché vive entre tests; reutilizar un nombre con
contenido distinto entre dos tests rompe esa invariante y un test ve el
resultado cacheado de OTRO — no es un bug del código, es el mismo tipo de
colisión que el propio mecanismo está pensado para evitar en producción."""

from __future__ import annotations

import os

import pytest

import app


XML_EMPTY = "<FlexQueryResponse><FlexStatements></FlexStatements></FlexQueryResponse>"


def _mini_xml_dates(*totals: float) -> str:
    """XML con una cuenta ("U1") y una fila de NAV por cada total en
    `totals`, en fechas consecutivas a partir de 2026-01-01 — lo justo para
    que nav_series()/accruals() tengan columna "account" y, con >= 2
    fechas, para que build_series() no falle por "serie insuficiente"."""
    rows = "".join(
        f'<EquitySummaryByReportDateInBase accountId="U1" reportDate="202601{i+1:02d}"'
        f' total="{t}" cash="{t}" stock="0"/>'
        for i, t in enumerate(totals)
    )
    return (
        '<FlexQueryResponse><FlexStatements>'
        '<FlexStatement accountId="U1" fromDate="20260101" toDate="20260131">'
        f'<EquitySummaryInBase>{rows}</EquitySummaryInBase>'
        '</FlexStatement></FlexStatements></FlexQueryResponse>')


def _write(dirpath, name, xml=XML_EMPTY):
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(xml)
    return p


@pytest.fixture(autouse=True)
def _spy_on_parse(monkeypatch):
    """Cuenta cuántas veces se ejecuta de verdad el cuerpo de P.load() —
    eso es lo que la caché de `load_paths` debe evitar repetir."""
    calls = []
    orig = app.P.load

    def spy(paths):
        calls.append(tuple(paths))
        return orig(paths)

    monkeypatch.setattr(app.P, "load", spy)
    return calls


def test_load_paths_cache_survives_different_session_directories(tmp_path, _spy_on_parse):
    """Dos 'sesiones' (dos directorios temporales distintos, como los que
    genera tempfile.mkdtemp() en q4_storage.init_session_storage) con el
    MISMO fichero y el MISMO orden: el segundo load_paths() no debe volver
    a ejecutar P.load()."""
    d1, d2 = tmp_path / "s1", tmp_path / "s2"
    d1.mkdir()
    d2.mkdir()
    name = "case_survives_sessions.xml"
    p1 = (_write(d1, name),)
    p2 = (_write(d2, name),)

    app.load_paths(p1)
    app.load_paths(p2)
    assert len(_spy_on_parse) == 1


def test_load_paths_cache_distinguishes_different_file_sets(tmp_path, _spy_on_parse):
    d1 = tmp_path / "s1"
    d1.mkdir()
    pa = (_write(d1, "case_distinguishes_sets_a.xml"),)
    pb = (_write(d1, "case_distinguishes_sets_b.xml"),)

    app.load_paths(pa)
    app.load_paths(pb)
    assert len(_spy_on_parse) == 2   # ficheros distintos: cachés distintas, sin confundirse


def test_load_paths_cache_distinguishes_order_within_same_file_set(tmp_path, _spy_on_parse):
    """Órdenes distintos del MISMO conjunto de ficheros NUNCA deben compartir
    caché — P.load() deduplica solapes con keep="last", así que el orden
    puede cambiar el resultado (ver docstring de _stable_cache_key)."""
    d1, d2 = tmp_path / "s1", tmp_path / "s2"
    d1.mkdir()
    d2.mkdir()
    _write(d1, "case_order_a.xml")
    _write(d1, "case_order_b.xml")
    _write(d2, "case_order_a.xml")
    _write(d2, "case_order_b.xml")

    forward = (os.path.join(d1, "case_order_a.xml"), os.path.join(d1, "case_order_b.xml"))
    backward = (os.path.join(d2, "case_order_b.xml"), os.path.join(d2, "case_order_a.xml"))

    app.load_paths(forward)
    app.load_paths(backward)
    assert len(_spy_on_parse) == 2   # dos órdenes distintos: dos ejecuciones, ninguna reutilizada


def test_cached_attribute_survives_different_session_directories(tmp_path, monkeypatch):
    """Mismo motivo que test_load_paths_cache_survives_..., un nivel más
    arriba: _cached_attribute() envuelve `paths` en `_RawPaths` para que
    también él sobreviva entre sesiones — sin esto, load_paths() ya
    devolvía rápido pero E.attribute() se recalculaba entero igualmente."""
    calls = []
    monkeypatch.setattr(app.E, "attribute", lambda *a, **k: calls.append(1) or object())
    d1, d2 = tmp_path / "s1", tmp_path / "s2"
    d1.mkdir()
    d2.mkdir()
    name = "case_attr_survives_sessions.xml"
    xml = _mini_xml_dates(1000.0, 1010.0)
    p1 = app._RawPaths((_write(d1, name, xml),))
    p2 = app._RawPaths((_write(d2, name, xml),))

    app._cached_attribute(p1, "20260101", "20260102", None, None)
    app._cached_attribute(p2, "20260101", "20260102", None, None)
    assert len(calls) == 1


def test_cached_attribute_distinguishes_different_accounts(tmp_path, monkeypatch):
    """`_RawPaths` sólo transforma `paths` — `accounts` (otra tupla, en la
    misma llamada) sigue hasheando por su contenido real y no debe
    confundirse entre sí."""
    calls = []
    monkeypatch.setattr(app.E, "attribute", lambda *a, **k: calls.append(1) or object())
    d1 = tmp_path / "s1"
    d1.mkdir()
    name = "case_attr_distinguishes_accounts.xml"
    paths = app._RawPaths((_write(d1, name, _mini_xml_dates(1000.0, 1010.0)),))

    app._cached_attribute(paths, "20260101", "20260102", None, ("U1",))
    app._cached_attribute(paths, "20260101", "20260102", None, ("U2",))
    assert len(calls) == 2


def test_cached_series_inputs_shared_across_different_periods(tmp_path, monkeypatch):
    """La razón de ser de _cached_series_inputs(): years_table()/
    trailing_table() llaman a _attr() una vez por año/ventana — con el
    mismo (histórico, cuentas) pero periodos DISTINTOS. Sin esta caché
    aparte, cada llamada a _cached_attribute() (con su propia clave de
    start/end) recalculaba fx_matrix/nav_series/currency_buckets/accruals/
    flows sobre TODO el histórico una vez por cada una — aquí se prueba
    que, de verdad, sólo se ejecuta una vez para dos periodos distintos."""
    calls = []
    orig = app.E.precompute_series_inputs

    def spy(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    monkeypatch.setattr(app.E, "precompute_series_inputs", spy)

    d1 = tmp_path / "s1"
    d1.mkdir()
    name = "case_series_inputs_shared.xml"
    paths = app._RawPaths((_write(d1, name, _mini_xml_dates(1000.0, 1010.0, 1000.0)),))

    # Dos VENTANAS distintas (como years_table()/trailing_table() piden una
    # por año/ventana) — distinta clave en _cached_attribute(), pero deben
    # compartir el mismo precompute_series_inputs().
    a = app._cached_attribute(paths, "20260101", "20260102", None, None)
    b = app._cached_attribute(paths, "20260102", "20260103", None, None)
    assert len(calls) == 1
    assert a.total == pytest.approx(0.01)                   # 1010/1000 - 1
    assert b.total == pytest.approx(1000 / 1010 - 1)


def test_cached_trades_survives_different_session_directories(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(app.T, "build", lambda *a, **k: (calls.append(1), (None, None))[1])
    d1, d2 = tmp_path / "s1", tmp_path / "s2"
    d1.mkdir()
    d2.mkdir()
    name = "case_trades_survives_sessions.xml"
    p1 = app._RawPaths((_write(d1, name),))
    p2 = app._RawPaths((_write(d2, name),))

    app._cached_trades(p1, "20260101", "20260102", None)
    app._cached_trades(p2, "20260101", "20260102", None)
    assert len(calls) == 1


def test_cached_portfolio_survives_different_session_directories(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(app.T, "portfolio", lambda *a, **k: calls.append(1) or object())
    d1, d2 = tmp_path / "s1", tmp_path / "s2"
    d1.mkdir()
    d2.mkdir()
    name = "case_portfolio_survives_sessions.xml"
    p1 = app._RawPaths((_write(d1, name),))
    p2 = app._RawPaths((_write(d2, name),))

    app._cached_portfolio(p1, None, None, "patrimonio")
    app._cached_portfolio(p2, None, None, "patrimonio")
    assert len(calls) == 1


def test_stable_cache_key_preserves_order_not_sorted():
    key_forward = app._stable_cache_key(("/x/b.xml", "/y/a.xml"))
    key_backward = app._stable_cache_key(("/x/a.xml", "/y/b.xml"))
    assert key_forward == "b.xml\x00a.xml"
    assert key_backward == "a.xml\x00b.xml"
    assert key_forward != key_backward
