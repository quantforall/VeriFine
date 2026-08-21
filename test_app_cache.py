"""Tests de la clave de caché de `load_paths()` en app.py (§B del repaso de
rendimiento: la caché de proceso de `st.cache_data` debe sobrevivir entre
sesiones — recarga de pestaña, otra pestaña, contenedor reciclado — para el
MISMO histórico, no solo dentro de una sesión).

Importa `app` en "modo bare" (sin un ScriptRunContext de Streamlit real) —
funciona porque nada a nivel de módulo depende de una sesión activa; sólo
usamos aquí funciones puras (`load_paths`, `_stable_cache_key`)."""

from __future__ import annotations

import os

import pytest

import app


XML = "<FlexQueryResponse><FlexStatements></FlexStatements></FlexQueryResponse>"


def _write(dirpath, name):
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(XML)
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
    name = "Q1_default_20260101T000000Z_abc123.xml"
    p1 = (_write(d1, name),)
    p2 = (_write(d2, name),)

    app.load_paths(p1)
    app.load_paths(p2)
    assert len(_spy_on_parse) == 1


def test_load_paths_cache_distinguishes_different_file_sets(tmp_path, _spy_on_parse):
    d1 = tmp_path / "s1"
    d1.mkdir()
    pa = (_write(d1, "a.xml"),)
    pb = (_write(d1, "b.xml"),)

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
    _write(d1, "a.xml")
    _write(d1, "b.xml")
    _write(d2, "a.xml")
    _write(d2, "b.xml")

    forward = (os.path.join(d1, "a.xml"), os.path.join(d1, "b.xml"))
    backward = (os.path.join(d2, "b.xml"), os.path.join(d2, "a.xml"))

    app.load_paths(forward)
    app.load_paths(backward)
    assert len(_spy_on_parse) == 2   # dos órdenes distintos: dos ejecuciones, ninguna reutilizada


def test_stable_cache_key_preserves_order_not_sorted():
    key_forward = app._stable_cache_key(("/x/b.xml", "/y/a.xml"))
    key_backward = app._stable_cache_key(("/x/a.xml", "/y/b.xml"))
    assert key_forward == "b.xml\x00a.xml"
    assert key_backward == "a.xml\x00b.xml"
    assert key_forward != key_backward
