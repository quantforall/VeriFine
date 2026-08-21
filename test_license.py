"""Tests de control de acceso por suscripción (q4_license), en dos niveles
(full/free).

Sólo cubre `evaluate`, la parte pura (sin red) — `fetch_valid_codes` es un
`requests.get` de una línea, no hay lógica que valga la pena testear ahí
aparte de la propia librería. `MANIFEST_URL` se fuerza con monkeypatch en vez
de la variable de entorno porque el módulo ya la leyó al importar.
"""

from __future__ import annotations

import q4_license as L

CODES = {"full": ["VF-2026-08"], "free": ["VF-FREE"]}


def test_license_disabled_without_manifest_url(monkeypatch):
    """Sin Q4_LICENSE_MANIFEST_URL configurada, no bloquea nada — ni con
    código vacío ni con uno que no está en ninguna lista. Nivel "full"."""
    monkeypatch.setattr(L, "MANIFEST_URL", "")
    ok, tier, level, msg = L.evaluate(L.LicenseState(), None)
    assert ok and tier == "full" and level == "" and msg == ""


def test_license_requires_code_when_enabled(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    ok, tier, level, msg = L.evaluate(L.LicenseState(code=""), CODES)
    assert not ok and tier == "" and level == "error" and "licencia" in msg.lower()


def test_license_full_code_authorizes_and_stamps_last_ok_and_tier(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    monkeypatch.setattr(L.dt, "date", _FixedDate)
    state = L.LicenseState(code="VF-2026-08")
    ok, tier, level, msg = L.evaluate(state, CODES)
    assert ok and tier == "full" and level == "" and msg == ""
    assert state.last_ok == "2026-08-19"
    assert state.last_tier == "full"


def test_license_free_code_authorizes_at_free_tier(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    monkeypatch.setattr(L.dt, "date", _FixedDate)
    state = L.LicenseState(code="VF-FREE")
    ok, tier, level, msg = L.evaluate(state, CODES)
    assert ok and tier == "free" and level == "" and msg == ""
    assert state.last_ok == "2026-08-19"
    assert state.last_tier == "free"


def test_license_full_wins_if_code_in_both_lists(monkeypatch):
    """Caso defensivo, no debería pasar en producción: si el mismo código
    apareciera por error en ambas listas del Gist, gana el nivel más alto."""
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    state = L.LicenseState(code="VF-DUP")
    ok, tier, level, msg = L.evaluate(state, {"full": ["VF-DUP"], "free": ["VF-DUP"]})
    assert ok and tier == "full"


def test_license_invalid_code_rejected(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    ok, tier, level, msg = L.evaluate(L.LicenseState(code="NOPE"), CODES)
    assert not ok and tier == "" and level == "error"


def test_license_stale_code_is_rejected_even_within_would_be_grace(monkeypatch):
    """Un código que ya no está en ninguna lista bloquea de inmediato — el
    margen de gracia es solo para cuando no se pudo CONSULTAR la lista, no
    para cuando se consultó y el código salió inválido."""
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    state = L.LicenseState(code="VF-2026-01", last_ok="2026-08-18", last_tier="full")
    ok, tier, level, msg = L.evaluate(state, CODES)
    assert not ok and tier == "" and level == "error"


def test_license_offline_within_grace_authorizes_with_warning_same_tier(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    monkeypatch.setattr(L, "GRACE_DAYS", 14)
    monkeypatch.setattr(L.dt, "date", _FixedDate)
    state = L.LicenseState(code="VF-FREE", last_ok="2026-08-10", last_tier="free")  # hace 9 días
    ok, tier, level, msg = L.evaluate(state, None)  # None = no se pudo consultar
    assert ok and tier == "free" and level == "warning" and "gracia" in msg.lower()


def test_license_offline_past_grace_blocks(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    monkeypatch.setattr(L, "GRACE_DAYS", 14)
    monkeypatch.setattr(L.dt, "date", _FixedDate)
    state = L.LicenseState(code="VF-2026-08", last_ok="2026-07-01", last_tier="full")  # hace 49 días
    ok, tier, level, msg = L.evaluate(state, None)
    assert not ok and tier == "" and level == "error" and "gracia" in msg.lower()


def test_license_offline_never_verified_blocks(monkeypatch):
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    state = L.LicenseState(code="VF-2026-08", last_ok="", last_tier="")
    ok, tier, level, msg = L.evaluate(state, None)
    assert not ok and tier == ""


def test_license_offline_without_last_tier_blocks_even_within_grace(monkeypatch):
    """Estado guardado de ANTES de que existiera last_tier (o corrupto): sin
    saber a qué nivel autorizar, no hay margen de gracia posible."""
    monkeypatch.setattr(L, "MANIFEST_URL", "https://example.com/manifest.json")
    monkeypatch.setattr(L, "GRACE_DAYS", 14)
    monkeypatch.setattr(L.dt, "date", _FixedDate)
    state = L.LicenseState(code="VF-2026-08", last_ok="2026-08-10", last_tier="")
    ok, tier, level, msg = L.evaluate(state, None)
    assert not ok and tier == ""


def test_license_state_roundtrips_tier(tmp_path):
    path = str(tmp_path / "license.json")
    L.LicenseState(code="VF-FREE", last_ok="2026-08-19", last_tier="free").save(path)
    loaded = L.LicenseState.load(path)
    assert loaded.code == "VF-FREE"
    assert loaded.last_tier == "free"


class _FixedDate(L.dt.date):
    """`dt.date` con `.today()` fijado a 2026-08-19, para que los tests no
    dependan de la fecha real de ejecución."""
    @classmethod
    def today(cls):
        return cls(2026, 8, 19)
