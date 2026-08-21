"""Tests del orquestador de sincronización (q4_sync.py) — sin red, con un
FlexClient simulado. Antes de esto el módulo no tenía tests dedicados; se
cubre aquí junto con el nuevo gancho `on_saved` de `backfill()` (ver su
docstring) que usará la capa de Google Drive (q4_storage.py)."""

from __future__ import annotations

import json

import pytest

import q4_sync as S
from q4_ingest import TokenExpired, StatementNotAvailable, FlexError


class FakeClient:
    """Sólo implementa lo que q4_sync.py usa: latest_available_date() y
    fetch(fd, td). `fetch_fn` puede lanzar para simular errores de IBKR."""
    def __init__(self, latest=None, fetch_fn=None):
        self._latest = latest
        self._fetch_fn = fetch_fn or (lambda fd, td: f"path_{fd}_{td}.xml")
        self.fetch_calls: list[tuple[str, str]] = []

    def latest_available_date(self, on_progress=None):
        return self._latest

    def fetch(self, fd=None, td=None, on_progress=None):
        self.fetch_calls.append((fd, td))
        return self._fetch_fn(fd, td)


# --------------------------------------------------------------------------
# SyncState
# --------------------------------------------------------------------------

def test_syncstate_load_missing_returns_blank_with_query_id():
    st = S.SyncState.load("/no/existe/state.json", "Q1")
    assert st.query_id == "Q1" and st.watermark == "" and st.windows_done == []


def test_syncstate_save_load_roundtrip(tmp_path):
    p = str(tmp_path / "state_Q1.json")
    st = S.SyncState(query_id="Q1", watermark="20260101",
                     windows_done=[["20250101", "20260101", "a.xml"]])
    st.save(p)
    loaded = S.SyncState.load(p, "Q1")
    assert loaded.watermark == "20260101"
    assert loaded.windows_done == [["20250101", "20260101", "a.xml"]]
    assert json.loads(open(p).read())["query_id"] == "Q1"


# --------------------------------------------------------------------------
# backfill(): reanudación y el nuevo gancho on_saved
# --------------------------------------------------------------------------

def test_backfill_calls_on_saved_once_per_pending_window(tmp_path):
    p = str(tmp_path / "state.json")
    st = S.SyncState(query_id="Q1")
    client = FakeClient()
    windows = S.plan_windows("20200101", "20260101", lead_days=S.LEAD_DAYS)

    seen = []
    S.backfill(client, st, p, "20200101", end="20260101", on_saved=lambda s: seen.append(len(s.windows_done)))

    assert len(client.fetch_calls) == len(windows)
    assert len(seen) == len(windows)
    assert seen == list(range(1, len(windows) + 1))  # una llamada tras CADA ventana, en orden


def test_backfill_without_on_saved_still_works(tmp_path):
    p = str(tmp_path / "state.json")
    st = S.SyncState(query_id="Q1")
    client = FakeClient()
    result = S.backfill(client, st, p, "20260101", end="20260101")
    assert len(result.windows_done) == 1
    assert result.last_run


def test_backfill_skips_windows_already_done(tmp_path):
    p = str(tmp_path / "state.json")
    windows = S.plan_windows("20200101", "20260101", lead_days=S.LEAD_DAYS)
    first_fd = windows[0][0]
    st = S.SyncState(query_id="Q1", history_start="20200101",
                     windows_done=[[first_fd, windows[0][1], "already.xml"]])
    client = FakeClient()
    S.backfill(client, st, p, "20200101", end="20260101")
    fetched_fds = {fd for fd, _ in client.fetch_calls}
    assert first_fd not in fetched_fds
    assert len(st.windows_done) == len(windows)  # el resto sí se completó


def test_backfill_token_expired_saves_paused_reason_and_reraises(tmp_path):
    p = str(tmp_path / "state.json")
    st = S.SyncState(query_id="Q1")

    def fetch_fn(fd, td):
        raise TokenExpired("1012", "expirado")

    client = FakeClient(fetch_fn=fetch_fn)
    with pytest.raises(TokenExpired):
        S.backfill(client, st, p, "20260101", end="20260101")
    assert st.paused_reason
    assert S.SyncState.load(p, "Q1").paused_reason  # se guardó en disco


# --------------------------------------------------------------------------
# incremental()
# --------------------------------------------------------------------------

def test_incremental_no_watermark_is_error(tmp_path):
    p = str(tmp_path / "state.json")
    st = S.SyncState(query_id="Q1")
    res = S.incremental(FakeClient(latest="20260101"), st, p)
    assert res["status"] == "error"


def test_incremental_paused_short_circuits(tmp_path):
    p = str(tmp_path / "state.json")
    st = S.SyncState(query_id="Q1", watermark="20260101", paused_reason="token caducado")
    res = S.incremental(FakeClient(latest="20260102"), st, p)
    assert res["status"] == "paused"


def test_incremental_no_new_data_when_latest_not_newer(tmp_path):
    p = str(tmp_path / "state.json")
    st = S.SyncState(query_id="Q1", watermark="20260101")
    res = S.incremental(FakeClient(latest="20260101"), st, p)
    assert res["status"] == "no_new_data"


def test_incremental_ok_returns_raw_path_and_saves(tmp_path):
    p = str(tmp_path / "state.json")
    st = S.SyncState(query_id="Q1", watermark="20260101")
    client = FakeClient(latest="20260105", fetch_fn=lambda fd, td: "new.xml")
    res = S.incremental(client, st, p)
    assert res["status"] == "ok" and res["raw_path"] == "new.xml"
    assert st.last_run and st.last_error == ""


def test_incremental_gap_too_large(tmp_path):
    p = str(tmp_path / "state.json")
    st = S.SyncState(query_id="Q1", watermark="20200101")
    res = S.incremental(FakeClient(latest="20260101"), st, p)
    assert res["status"] == "gap_too_large"


# --------------------------------------------------------------------------
# update_watermark / check_nav_conflicts / check_golden
# --------------------------------------------------------------------------

def test_update_watermark_only_advances_forward(tmp_path):
    p = str(tmp_path / "state.json")
    st = S.SyncState(query_id="Q1", watermark="20260105")
    S.update_watermark(st, p, ["20260101", "20260103"])   # más antiguo: no retrocede
    assert st.watermark == "20260105"
    S.update_watermark(st, p, ["20260110"])
    assert st.watermark == "20260110"


def test_check_nav_conflicts_detects_mismatch():
    conflicts = S.check_nav_conflicts({"20260101": 100.0}, {"20260101": 100.5})
    assert conflicts == [("20260101", 100.0, 100.5)]


def test_check_nav_conflicts_within_tolerance_is_clean():
    assert S.check_nav_conflicts({"20260101": 100.0}, {"20260101": 100.0000001}) == []


def test_check_golden_seeds_on_first_run(tmp_path):
    p = str(tmp_path / "state.json")
    st = S.SyncState(query_id="Q1")
    drift = S.check_golden(st, p, {"2025": 7.5})
    assert drift == [] and st.golden == {"2025": 7.5}


def test_check_golden_detects_drift():
    st = S.SyncState(query_id="Q1", golden={"2025": 7.5})
    drift = S.check_golden(st, "/unused", {"2025": 7.9}, tol_bp=1.0)
    assert drift and "2025" in drift[0]


# --------------------------------------------------------------------------
# daily_job()
# --------------------------------------------------------------------------

def test_daily_job_ok_updates_watermark_and_checks_golden(tmp_path):
    p = str(tmp_path / "state.json")
    S.SyncState(query_id="Q1", watermark="20260101").save(p)
    client = FakeClient(latest="20260102", fetch_fn=lambda fd, td: "new.xml")

    res = S.daily_job(client, p, "Q1",
                      parse_fn=lambda path: ({"20260102": 100.0}, ["20260102"]),
                      recompute_fn=lambda: {"2026": 1.0})
    assert res["status"] == "ok"
    assert res["new_dates"] == ["20260102"]
    assert S.SyncState.load(p, "Q1").watermark == "20260102"
    # Regresión: sin `raw_path` en el resultado, app.py nunca sube a Drive el
    # XML nuevo ni el state.json con el watermark actualizado — la sesión
    # siguiente (rehidratada desde Drive) veía el watermark VIEJO aunque la
    # sincronización hubiera ido bien en local (ver app.py `_run_incremental_sync`).
    assert res["raw_path"] == "new.xml"


def test_daily_job_no_new_dates_from_parse(tmp_path):
    p = str(tmp_path / "state.json")
    S.SyncState(query_id="Q1", watermark="20260101").save(p)
    client = FakeClient(latest="20260102", fetch_fn=lambda fd, td: "new.xml")

    res = S.daily_job(client, p, "Q1",
                      parse_fn=lambda path: ({}, []),
                      recompute_fn=lambda: {})
    assert res["status"] == "no_new_data"
    # También aquí: el XML ya se descargó, aunque resultara sin fechas
    # nuevas — sin `raw_path` se queda huérfano en el RAW_DIR de la sesión.
    assert res["raw_path"] == "new.xml"
