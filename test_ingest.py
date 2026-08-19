"""Tests del cliente de ingesta (§18) con transporte simulado — sin red.

Cubren en particular los caminos que fallaron contra IBKR real en la puesta en
marcha: 1003 (statement no disponible), 1018/1025 (límites de ritmo), errores
de red (connection reset), y la alineación de los cortes de bloque a día hábil.
"""

from __future__ import annotations

import datetime as dt

import pytest
import requests

import q4_ingest as Q


# --------------------------------------------------------------------------
# Transporte simulado
# --------------------------------------------------------------------------

class FakeResp:
    def __init__(self, text="", status_code=200, url="https://x?t=SECRET&q=1&v=3"):
        self.text, self.status_code, self.url = text, status_code, url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class ScriptedSession:
    """Devuelve/lanza lo que diga el guion, una entrada por llamada a get()."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):          # (status_code, text)
            return FakeResp(item[1], item[0])
        return FakeResp(item, 200)           # texto -> 200


def env(status, code=None, msg=None, ref=None, url=None):
    p = [f"<Status>{status}</Status>"]
    if code: p.append(f"<ErrorCode>{code}</ErrorCode>")
    if msg: p.append(f"<ErrorMessage>{msg}</ErrorMessage>")
    if ref: p.append(f"<ReferenceCode>{ref}</ReferenceCode>")
    if url: p.append(f"<Url>{url}</Url>")
    return "<FlexStatementResponse>" + "".join(p) + "</FlexStatementResponse>"


SUCCESS = env("Success", ref="R1", url="https://x/Get")
STATEMENT = "<FlexQueryResponse><FlexStatements count='1'/></FlexQueryResponse>"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(Q.time, "sleep", lambda *a, **k: None)


def make_client(script):
    c = Q.FlexClient(token="SECRET", query_id="123", session=ScriptedSession(script))
    c._pacer.wait = lambda: None
    return c


# --------------------------------------------------------------------------
# Seguridad y utilidades
# --------------------------------------------------------------------------

def test_redact_hides_token():
    red = Q._redact("https://x/SendRequest?t=SECRET&q=1&v=3")
    assert "SECRET" not in red and "<redacted>" in red


def test_prev_weekday():
    assert Q._prev_weekday(dt.datetime(2022, 12, 31)).strftime("%Y%m%d") == "20221230"  # sáb->vie
    assert Q._prev_weekday(dt.datetime(2023, 12, 31)).strftime("%Y%m%d") == "20231229"  # dom->vie
    assert Q._prev_weekday(dt.datetime(2024, 12, 30)).strftime("%Y%m%d") == "20241230"  # lun


# --------------------------------------------------------------------------
# Planificación de bloques
# --------------------------------------------------------------------------

def test_plan_windows_td_on_weekdays_and_bounded():
    ws = Q.plan_windows("20210106", "20260814", lead_days=5)
    assert ws[0][0] == "20210101"                 # primer bloque adelantado 5 días
    assert ws[-1][1] == "20260814"                # cubre hasta el final (viernes)
    for fd, td in ws:
        d1 = dt.datetime.strptime(td, "%Y%m%d")
        assert d1.weekday() < 5, f"td {td} cae en fin de semana"
        span = (d1 - dt.datetime.strptime(fd, "%Y%m%d")).days
        assert span <= Q.MAX_WINDOW_DAYS
    fds = [fd for fd, _ in ws]
    assert fds == sorted(fds)                      # bloques en orden, sin solaparse


# --------------------------------------------------------------------------
# Protocolo: SendRequest
# --------------------------------------------------------------------------

def test_request_success():
    c = make_client([SUCCESS])
    assert c.request_statement() == ("R1", "https://x/Get")


def test_request_retries_1018_then_ok():
    c = make_client([env("Warn", code="1018", msg="too many requests"), SUCCESS])
    assert c.request_statement() == ("R1", "https://x/Get")
    assert len(c.session.calls) == 2              # reintentó el 1018


def test_request_1025_raises_blocked():
    c = make_client([env("Fail", code="1025", msg="too many failed attempts")])
    with pytest.raises(Q.RateLimitBlocked):
        c.request_statement()


def test_request_1012_raises_token_expired():
    c = make_client([env("Fail", code="1012", msg="expired")])
    with pytest.raises(Q.TokenExpired):
        c.request_statement()


def test_request_1003_raises_not_available():
    c = make_client([env("Warn", code="1003", msg="not available")])
    with pytest.raises(Q.StatementNotAvailable):
        c.request_statement()


def test_request_range_over_365_rejected():
    c = make_client([SUCCESS])
    with pytest.raises(ValueError):
        c.request_statement("20240101", "20260101")


# --------------------------------------------------------------------------
# Protocolo: get_statement (sondeo)
# --------------------------------------------------------------------------

def test_get_statement_polls_then_returns():
    c = make_client([env("Warn", code="1019", msg="generating"), STATEMENT])
    xml = c.get_statement("https://x/Get", "R1")
    assert "FlexQueryResponse" in xml
    assert len(c.session.calls) == 2


def test_get_statement_1025_raises_blocked():
    c = make_client([env("Fail", code="1025", msg="blocked")])
    with pytest.raises(Q.RateLimitBlocked):
        c.get_statement("https://x/Get", "R1")


# --------------------------------------------------------------------------
# Reintento de RED (lo que nos tumbó: connection reset)
# --------------------------------------------------------------------------

def test_network_reset_is_retried():
    c = make_client([requests.ConnectionError("reset by peer"), SUCCESS])
    assert c.request_statement() == ("R1", "https://x/Get")
    assert len(c.session.calls) == 2


def test_network_5xx_is_retried():
    c = make_client([(503, "busy"), SUCCESS])
    assert c.request_statement() == ("R1", "https://x/Get")


def test_network_exhausted_raises_flexerror_net():
    c = make_client([requests.ConnectionError("reset")] * Q.NET_RETRIES)
    with pytest.raises(Q.FlexError) as ei:
        c.request_statement()
    assert ei.value.code == "NET"


# --------------------------------------------------------------------------
# Incremental: descubrir último cierre + guarda de 365 días
# --------------------------------------------------------------------------

def test_incremental_none_when_nothing_new(monkeypatch):
    c = make_client([])
    monkeypatch.setattr(c, "latest_available_date", lambda: "20260814")
    assert Q.incremental(c, "20260814") is None      # latest <= last_date


def test_incremental_fetches_capped_range(monkeypatch):
    c = make_client([])
    monkeypatch.setattr(c, "latest_available_date", lambda: "20260814")
    seen = {}
    monkeypatch.setattr(c, "fetch", lambda fd, td: seen.update(fd=fd, td=td) or "path")
    assert Q.incremental(c, "20260810", overlap_days=10) == "path"
    assert seen == {"fd": "20260731", "td": "20260814"}  # watermark-10 → último cierre


def test_incremental_gap_too_large_raises(monkeypatch):
    c = make_client([])
    monkeypatch.setattr(c, "latest_available_date", lambda: "20260814")
    with pytest.raises(ValueError):
        Q.incremental(c, "20240101")                 # hueco > 365 días


# --------------------------------------------------------------------------
# validate_query (§18.5)
# --------------------------------------------------------------------------

def _query_xml(sections, pos_dates):
    body = "".join(f"<{s}/>" for s in sections if s not in ("EquitySummaryInBase", "OpenPositions"))
    eqs = "".join(f'<EquitySummaryByReportDateInBase reportDate="{d}"/>' for d in pos_dates)
    ops = "".join(f'<OpenPosition reportDate="{d}"/>' for d in pos_dates)
    extra = ""
    if "EquitySummaryInBase" in sections:
        extra += f"<EquitySummaryInBase>{eqs}</EquitySummaryInBase>"
    if "OpenPositions" in sections:
        extra += f"<OpenPositions>{ops}</OpenPositions>"
    return (f'<FlexQueryResponse><FlexStatements><FlexStatement accountId="U1">'
            f'{body}{extra}</FlexStatement></FlexStatements></FlexQueryResponse>')


def test_validate_query_ok():
    xml = _query_xml(Q.REQUIRED_SECTIONS, ["20250102", "20250103"])
    v = Q.validate_query(xml)
    assert v["ok"] and not v["missing_sections"] and v["accounts"] == ["U1"]
    assert v["daily_positions"] is True


def test_validate_query_missing_section():
    xml = _query_xml([s for s in Q.REQUIRED_SECTIONS if s != "Trades"], ["20250102", "20250103"])
    v = Q.validate_query(xml)
    assert not v["ok"] and "Trades" in v["missing_sections"]


def test_validate_query_single_snapshot_detected():
    xml = _query_xml(Q.REQUIRED_SECTIONS, ["20250102"])   # una sola fecha de posiciones
    v = Q.validate_query(xml)
    assert not v["ok"] and v["daily_positions"] is False
