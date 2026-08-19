"""Tests del auto-chequeo de integridad (q4_selfcheck)."""

from __future__ import annotations

import q4_parser as P
import q4_selfcheck as SC
from test_parser import _mini_xml


def test_selfcheck_clean_dataset(tmp_path):
    """Datos que reconstruyen: sin hallazgos."""
    p = tmp_path / "clean.xml"
    p.write_text(_mini_xml(total=800, cash=300, stock=0, options=500,
                           pos_cat="OPT", pos_ccy="USD", pos_value=500, fx_eur_cash=300))
    assert SC.run_checks(P.load([str(p)])) == []


def test_selfcheck_flags_nav_that_does_not_reconstruct(tmp_path):
    """Si el NAV no reconstruye (aquí, `stock` del summary no cuadra con la
    posición), el auto-chequeo lo marca con un error T6b en vez de callar."""
    # summary dice stock=500 pero la posición vale 1000 -> residuo de 500
    p = tmp_path / "broken.xml"
    p.write_text(_mini_xml(total=1000, cash=0, stock=500, options=0,
                           pos_cat="STK", pos_ccy="USD", pos_value=1000))
    findings = SC.run_checks(P.load([str(p)]))
    assert any(f["check"] == "T6b" and f["level"] == "error" for f in findings)
