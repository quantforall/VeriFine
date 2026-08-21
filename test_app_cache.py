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
import json
import time
import hashlib

import pytest

import app


XML_EMPTY = "<FlexQueryResponse><FlexStatements></FlexStatements></FlexQueryResponse>"


def _mini_xml_dates(*totals: float) -> str:
    """XML con una cuenta ("U1") y una fila de NAV por cada total en
    `totals`, en fechas consecutivas a partir de 2026-01-01 — lo justo para
    que nav_series()/accruals() tengan columna "account" y, con >= 2
    fechas, para que build_series() no falle por "serie insuficiente". Con
    tipo de cambio USD (aunque no se use ninguna posición en USD) para que
    SC.run_checks() —que compara EUR vs. USD para T4— no falle por falta
    de tipos de cambio."""
    rows = "".join(
        f'<EquitySummaryByReportDateInBase accountId="U1" reportDate="202601{i+1:02d}"'
        f' total="{t}" cash="{t}" stock="0"/>'
        for i, t in enumerate(totals)
    )
    fx_rows = "".join(
        f'<ConversionRate reportDate="202601{i+1:02d}" fromCurrency="USD" '
        'toCurrency="EUR" rate="1.1"/>'
        for i in range(len(totals))
    )
    return (
        '<FlexQueryResponse><FlexStatements>'
        '<FlexStatement accountId="U1" fromDate="20260101" toDate="20260131">'
        f'<EquitySummaryInBase>{rows}</EquitySummaryInBase>'
        f'<ConversionRates>{fx_rows}</ConversionRates>'
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

    def spy(paths, **kwargs):
        calls.append(tuple(paths))
        return orig(paths, **kwargs)

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


def test_cached_selfcheck_survives_different_session_directories(tmp_path, monkeypatch):
    """Regresión: el guard `id(ds) != st.session_state["_sc_id"]` que había
    antes de _cached_selfcheck() nunca acertaba (`st.cache_data` devuelve
    una copia nueva en cada llamada, `id()` distinto siempre) — SC.run_checks()
    se repetía en cada rerun. Aquí se prueba que, de verdad, se ejecuta una
    sola vez para el mismo histórico."""
    calls = []
    orig = app.SC.run_checks

    def spy(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    monkeypatch.setattr(app.SC, "run_checks", spy)
    d1, d2 = tmp_path / "s1", tmp_path / "s2"
    d1.mkdir()
    d2.mkdir()
    name = "case_selfcheck_survives_sessions.xml"
    xml = _mini_xml_dates(1000.0, 1010.0)
    p1 = app._RawPaths((_write(d1, name, xml),))
    p2 = app._RawPaths((_write(d2, name, xml),))

    app._cached_selfcheck(p1)
    app._cached_selfcheck(p2)
    assert len(calls) == 1


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


class FakeSelfcheckDrive:
    """Doble mínimo de DriveFolder — sólo lo que usa
    _selfcheck_with_drive_cache (download/upload)."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def download(self, name):
        return self.files.get(name)

    def upload(self, name, content, mime_type="application/octet-stream"):
        self.files[name] = content
        return name


def test_selfcheck_with_drive_cache_returns_persisted_artifact_without_recomputing(
        tmp_path, monkeypatch):
    """Fase 4 del plan de escalabilidad: si el artefacto de Drive coincide
    con la huella del histórico actual, ni siquiera se llama a
    SC.run_checks() -- el punto entero es saltarse ese cálculo en un
    proceso frío."""
    calls = []
    monkeypatch.setattr(app.SC, "run_checks", lambda *a, **k: calls.append(1) or [])
    d1 = tmp_path / "s1"
    d1.mkdir()
    paths = (_write(d1, "case_drive_cache_hit.xml"),)
    fingerprint = app._stable_cache_key(paths)

    drive = FakeSelfcheckDrive()
    drive.files[app._SELFCHECK_ARTIFACT_NAME] = json.dumps(
        {"fingerprint": fingerprint, "issues": [{"level": "warning", "msg": "desde Drive"}]}
    ).encode("utf-8")
    app.st.session_state["_drive_folder"] = drive
    try:
        issues = app._selfcheck_with_drive_cache(paths)
    finally:
        app.st.session_state.pop("_drive_folder", None)

    assert issues == [{"level": "warning", "msg": "desde Drive"}]
    assert calls == []


def test_selfcheck_with_drive_cache_falls_back_and_persists_on_mismatch(tmp_path, monkeypatch):
    """Huella distinta (histórico distinto, o artefacto de otra sesión
    vieja) -- se recalcula de verdad y el resultado NUEVO se sube a Drive
    en segundo plano para la próxima vez."""
    monkeypatch.setattr(app.SC, "run_checks", lambda *a, **k: [])
    d1 = tmp_path / "s1"
    d1.mkdir()
    paths = (_write(d1, "case_drive_cache_miss.xml"),)
    fingerprint = app._stable_cache_key(paths)

    drive = FakeSelfcheckDrive()
    drive.files[app._SELFCHECK_ARTIFACT_NAME] = json.dumps(
        {"fingerprint": "huella-de-otro-histórico",
         "issues": [{"level": "error", "msg": "no debería verse"}]}
    ).encode("utf-8")
    app.st.session_state["_drive_folder"] = drive
    try:
        issues = app._selfcheck_with_drive_cache(paths)
        # el hilo de persistencia es un daemon en background -- darle un
        # instante para que escriba antes de comprobar (con tope, no un
        # sleep fijo, para no ser más lento de lo necesario en CI).
        for _ in range(200):
            saved_raw = drive.files.get(app._SELFCHECK_ARTIFACT_NAME)
            if saved_raw and json.loads(saved_raw)["fingerprint"] == fingerprint:
                break
            time.sleep(0.01)
    finally:
        app.st.session_state.pop("_drive_folder", None)

    assert issues == []   # el resultado RECALCULADO (mock), no el del artefacto viejo
    saved = json.loads(drive.files[app._SELFCHECK_ARTIFACT_NAME])
    assert saved["fingerprint"] == fingerprint
    assert saved["issues"] == []


def test_selfcheck_with_drive_cache_works_without_drive(tmp_path, monkeypatch):
    """Q4_STORAGE_BACKEND=local (sin Drive conectado): cae directo en
    _cached_selfcheck(), sin intentar leer ni escribir ningún artefacto."""
    monkeypatch.setattr(app.SC, "run_checks", lambda *a, **k: [])
    d1 = tmp_path / "s1"
    d1.mkdir()
    paths = (_write(d1, "case_drive_cache_no_drive.xml"),)
    app.st.session_state.pop("_drive_folder", None)
    assert app._selfcheck_with_drive_cache(paths) == []


def test_stable_cache_key_preserves_order_not_sorted():
    key_forward = app._stable_cache_key(("/x/b.xml", "/y/a.xml"))
    key_backward = app._stable_cache_key(("/x/a.xml", "/y/b.xml"))
    assert key_forward == "b.xml\x00a.xml"
    assert key_backward == "a.xml\x00b.xml"
    assert key_forward != key_backward


def _flex_filename(query_id: str, xml: str, tag: str = "default",
                   stamp: str = "20260101T000000Z") -> str:
    """Reproduce el esquema de nombre REAL de q4_ingest.FlexClient.fetch():
    `{query_id}_{tag}_{stamp}_{sha256(contenido)[:12]}.xml`. No importa
    q4_ingest aquí a propósito -- este test no quiere una llamada de red,
    sólo verificar el INVARIANTE del que depende _stable_cache_key (ver su
    docstring): si esta fórmula cambiara alguna vez en q4_ingest.py sin
    tocar aquí, el test seguiría siendo válido como documentación de lo que
    _stable_cache_key ASUME, aunque dejara de reflejar el código real -- por
    eso conviene revisar este test si `FlexClient.fetch()` cambia de
    esquema."""
    digest = hashlib.sha256(xml.encode()).hexdigest()[:12]
    return f"{query_id}_{tag}_{stamp}_{digest}.xml"


def test_stable_cache_key_does_not_collide_across_different_content(tmp_path, _spy_on_parse):
    """Invariante de aislamiento de la Fase 0 del plan de escalabilidad: dos
    'usuarios' que compartieran el MISMO query_id de IBKR (el peor caso: en
    producción el query_id ya es una credencial privada por cuenta, así que
    esto ni siquiera debería pasar) pero con historiales DISTINTOS nunca
    comparten entrada de caché, porque el nombre de fichero real incluye un
    hash del contenido (ver `_flex_filename` / `q4_ingest.FlexClient.fetch`).
    Sin este hash, el P0 que señalaba el diagnóstico externo (dos usuarios
    con nombre de fichero igual reutilizando el resultado del otro) sí sería
    un riesgo real -- con él, no lo es."""
    same_qid = "Q999"
    xml_user_a = _mini_xml_dates(1000.0, 1010.0)
    xml_user_b = _mini_xml_dates(5000.0, 4000.0)   # historial totalmente distinto

    name_a = _flex_filename(same_qid, xml_user_a)
    name_b = _flex_filename(same_qid, xml_user_b)
    assert name_a != name_b   # el hash de contenido ya los separa

    da, db = tmp_path / "user_a", tmp_path / "user_b"
    da.mkdir()
    db.mkdir()
    pa = (_write(da, name_a, xml_user_a),)
    pb = (_write(db, name_b, xml_user_b),)

    ds_a = app.load_paths(pa)
    ds_b = app.load_paths(pb)
    assert len(_spy_on_parse) == 2   # dos entradas de caché distintas, ninguna reutilizada
    # y de verdad son datos distintos, no sólo "no se compartió la llamada"
    assert set(ds_a.nav["total"]) != set(ds_b.nav["total"])


def test_tab_gate_blocks_only_the_blocked_tier():
    """Licencia de 3 niveles: Efecto divisa/Operaciones/Informe se
    desbloquean con "free" igual que con "full" -- sólo "blocked" (sin
    licencia válida) los oculta."""
    assert app._tab_gate("full") is True
    assert app._tab_gate("free") is True
    assert app._tab_gate("blocked") is False
