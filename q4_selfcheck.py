"""
VeriFine — auto-chequeo de integridad al cargar los datos de un usuario.

Ejecuta en runtime, sobre los datos del propio usuario, las mismas invariantes
que la batería de tests. Sirve de red de seguridad para las formas de cartera
que el motor aún no ha validado (opciones, bonos, base no-EUR, multi-cuenta con
activos raros…): en vez de mostrar números sutilmente mal en silencio, avisa.

Comprobaciones:
  T6b  el NAV de cada cuenta reconstruye desde los cubos por divisa.
  T5   la atribución cierra: Estrategia × Divisa = Total.
  T4   la estrategia es idéntica en EUR y en USD.
  §9   ningún salto diario > 20 % sin flujo de capital (split/corporate action).

Devuelve una lista de hallazgos; vacía = todo cuadra. Cada hallazgo:
    {level: 'error'|'warning', check: str, account: str|None, msg: str}
"""

from __future__ import annotations

import q4_parser as P
import q4_engine as E

REC_ABS = 1.0          # € de residuo de reconstrucción admisible fuera del bootstrap
REC_REL = 1e-5         # o esta fracción del NAV, lo que sea mayor
SPLIT_R = 0.20         # |r| diario que dispara sospecha de split sin flujo (§9)
MAX_JUMPS = 3          # cuántos saltos §9 se listan antes de resumir


def run_checks(ds: P.Dataset, currency: str = "EUR") -> list[dict]:
    out: list[dict] = []
    if ds.nav.empty:
        return out
    fx = E.FX(P.fx_matrix(ds), ds.base_currency)
    boot = set(ds.bootstrapped)

    # T6b — el NAV de cada cuenta reconstruye desde los cubos por divisa.
    for a in ds.accounts:
        nav = P.nav_series(ds, [a])
        accr = P.accruals(ds, [a])
        buck = P.currency_buckets(ds, [a])
        worst, worst_d = 0.0, None
        for d, n in nav.items():
            if d in boot:                       # el día bootstrap es aproximado (§11.8)
                continue
            v = sum(x * fx.rate(c, d) for c, x in buck.get(d, {}).items()) + accr.get(d, 0.0)
            if abs(v - n) > worst:
                worst, worst_d = abs(v - n), d
        thr = max(REC_ABS, REC_REL * max(nav.values(), default=0.0))
        if worst > thr:
            out.append(dict(level="error", check="T6b", account=a,
                msg=(f"El NAV de {a} no reconstruye desde los cubos por divisa "
                     f"(residuo {worst:,.0f} € el {worst_d}). Suele indicar un tipo de "
                     "activo o divisa que el parser aún no captura (opciones, bonos, "
                     "otra moneda base): los números de esa cuenta pueden estar mal.")))

    # T5 / T4 / §9 — sobre el consolidado.
    nav_all = P.nav_series(ds)
    dates = [d for d in sorted(nav_all) if nav_all[d] > 0]
    if len(dates) < 2:
        return out
    a0, a1 = dates[0], dates[-1]
    try:
        att = E.attribute(ds, a0, a1, analysis_currency=currency)
    except E.UndefinedReturn as e:
        out.append(dict(level="warning", check="§9", account=None,
                        msg=f"La serie tiene un tramo no calculable (§9): {e}"))
        return out

    if abs(att.closes()) > 1e-9:
        out.append(dict(level="error", check="T5", account=None,
            msg=(f"La atribución no cierra (residuo {att.closes():.1e}): "
                 "Estrategia × Divisa ≠ Total. Hay un error de cálculo.")))
    au = E.attribute(ds, a0, a1, analysis_currency="USD")
    if abs(att.strategy - au.strategy) > 1e-9:
        out.append(dict(level="error", check="T4", account=None,
            msg=(f"La estrategia difiere en EUR y USD (Δ {abs(att.strategy - au.strategy):.1e}); "
                 "debería ser idéntica. Hay un bug.")))

    fl = P.flows(ds, ds.accounts)
    jumps = [(d, r) for d, r in zip(att.series_total.dates[1:], att.series_total.returns)
             if abs(r) > SPLIT_R and not fl.get(d)]
    for d, r in jumps[:MAX_JUMPS]:
        out.append(dict(level="warning", check="§9", account=None,
            msg=(f"Salto de {r * 100:+.0f}% el {d} sin flujo de capital: posible split "
                 "o corporate action no reflejado en cantidad/precio (§9).")))
    if len(jumps) > MAX_JUMPS:
        out.append(dict(level="warning", check="§9", account=None,
            msg=f"…y {len(jumps) - MAX_JUMPS} saltos más > 20 % sin flujo."))
    return out
