"""Tests de control de acceso por suscripción (q4_license).

Sólo cubre `evaluate`, la parte pura (sin red) — `fetch_valid_codes` es un
`requests.get` de una línea, no hay lógica que valga la pena testear ahí
aparte de la propia librería. `MANIFEST_URL` se fuerza con monkeypatch en vez
de la variable de entorno porque el módulo ya la leyó al importar.
"""

from __future__ import annotations

import q4_license as L


def test_license_disabled_without_manifest_url(monkeypatch):
    """Sin Q4_LICENSE_MANIFEST_URL configurada, no bloquea nada — ni con
    código vacío ni con uno que no está en ninguna lista."""
    monkeypatch.setattr(L, "MANIFEST_URL", "")
    ok, level, msg = L.evaluate(L.LicenseState(), None)
    assert ok and level == "" and msg == ""


def test_license_requires_code_when_enabled(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    ok, level, msg = L.evaluate(L.LicenseState(code=""), ["VF-2026-08"])
    assert not ok and level == "error" and "código" in msg.lower()


def test_license_valid_code_authorizes_and_stamps_last_ok(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    monkeypatch.setattr(L.dt, "date", _FixedDate)
    state = L.LicenseState(code="VF-2026-08")
    ok, level, msg = L.evaluate(state, ["VF-2026-07", "VF-2026-08"])
    assert ok and level == "" and msg == ""
    assert state.last_ok == "2026-08-19"


def test_license_stale_code_is_rejected_even_within_would_be_grace(monkeypatch):
    """Un código que ya no está en la lista bloquea de inmediato — el margen
    de gracia es solo para cuando no se pudo CONSULTAR la lista, no para
    cuando se consultó y el código salió inválido."""
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    state = L.LicenseState(code="VF-2026-01", last_ok="2026-08-18")
    ok, level, msg = L.evaluate(state, ["VF-2026-08"])
    assert not ok and level == "error"


def test_license_offline_within_grace_authorizes_with_warning(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    monkeypatch.setattr(L, "GRACE_DAYS", 14)
    monkeypatch.setattr(L.dt, "date", _FixedDate)
    state = L.LicenseState(code="VF-2026-08", last_ok="2026-08-10")  # hace 9 días
    ok, level, msg = L.evaluate(state, None)  # None = no se pudo consultar
    assert ok and level == "warning" and "gracia" in msg.lower()


def test_license_offline_past_grace_blocks(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    monkeypatch.setattr(L, "GRACE_DAYS", 14)
    monkeypatch.setattr(L.dt, "date", _FixedDate)
    state = L.LicenseState(code="VF-2026-08", last_ok="2026-07-01")  # hace 49 días
    ok, level, msg = L.evaluate(state, None)
    assert not ok and level == "error" and "gracia" in msg.lower()


def test_license_offline_never_verified_blocks(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    state = L.LicenseState(code="VF-2026-08", last_ok="")
    ok, level, msg = L.evaluate(state, None)
    assert not ok and level == "error"


class _FixedDate(L.dt.date):
    """`dt.date` con `.today()` fijado a 2026-08-19, para que los tests no
    dependan de la fecha real de ejecución."""
    @classmethod
    def today(cls):
        return cls(2026, 8, 19)
