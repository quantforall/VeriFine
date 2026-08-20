"""
Quant4all — Orquestador de sincronización (§19 de la spec).

Dos modos sobre la MISMA Flex Query:

    backfill     una vez, histórico completo troceado en ventanas de <= 365 días
    incremental  cada día, últimos N días con solape hacia atrás

La diferencia es sólo el rango `fd`/`td`. No hay dos integraciones.

Estado persistido en un JSON junto al almacén de crudos, para que el backfill
sea reanudable y el incremental sepa desde dónde continuar.
"""

from __future__ import annotations

import os
import json
import logging
import datetime as dt
from dataclasses import dataclass, asdict, field

from q4_ingest import (FlexClient, TokenExpired, StatementNotAvailable,
                       RateLimitBlocked, FlexError, plan_windows, _prev_weekday,
                       MAX_WINDOW_DAYS)

log = logging.getLogger("q4.sync")

OVERLAP_DAYS = 10
LEAD_DAYS = 5


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------

@dataclass
class SyncState:
    query_id: str
    account_hint: str = ""
    history_start: str = ""          # primera fecha pedida al hacer backfill
    watermark: str = ""              # max(reportDate) REALMENTE ingerido
    windows_done: list = field(default_factory=list)   # [[fd, td, path], ...]
    last_run: str = ""
    last_error: str = ""
    paused_reason: str = ""          # p.ej. token caducado
    golden: dict = field(default_factory=dict)         # {"2023": 7.497979, ...}

    @classmethod
    def load(cls, path: str, query_id: str) -> "SyncState":
        if os.path.exists(path):
            return cls(**json.load(open(path)))
        return cls(query_id=query_id)

    def save(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        json.dump(asdict(self), open(path, "w"), indent=1, ensure_ascii=False)


# --------------------------------------------------------------------------
# Backfill reanudable
# --------------------------------------------------------------------------

MAX_TD_RETREAT = 4       # días hábiles que se retrocede el `td` ante un 1003 (festivos)


def _fetch_retreating(client: FlexClient, fd: str, td: str,
                      max_retreat: int = MAX_TD_RETREAT, on_poll_progress=None) -> tuple[str, str]:
    """Descarga un bloque retrocediendo el `td` si IBKR responde 1003.

    plan_windows ya evita los fines de semana; esto cubre el caso residual de
    un festivo entre semana en el corte del bloque. El retroceso salta
    directamente al día hábil anterior para no gastar intentos en sábados y
    domingos, y está acotado para no disparar el 1025 ("demasiados intentos
    fallidos") de IBKR. Devuelve (td_real, path).

    on_poll_progress: ver FlexClient.fetch() — solo pasa a través, nada
    propio de aquí."""
    d = dt.datetime.strptime(td, "%Y%m%d")
    for _ in range(max_retreat + 1):
        cur_td = d.strftime("%Y%m%d")
        try:
            return cur_td, client.fetch(fd, cur_td, on_progress=on_poll_progress)
        except StatementNotAvailable:
            d = _prev_weekday(d - dt.timedelta(days=1))
            log.info("1003 en el corte del bloque; retrocedo td a %s", d.strftime("%Y%m%d"))
    raise StatementNotAvailable("1003", f"Sin cierre disponible cerca de {td} para el bloque {fd}")


def backfill(client: FlexClient, state: SyncState, state_path: str,
             start: str, end: str | None = None, on_progress=None,
             on_poll_progress=None) -> SyncState:
    """Descarga el histórico completo. Reanudable: salta ventanas ya hechas.

    Un backfill de 6 años son 6 peticiones; si la cuarta falla, las tres
    primeras no se repiten. Cada bloque anual tarda en generarse en el servidor
    de IBKR y consume cuota, así que reintentar desde cero no es aceptable.

    La reanudación se indexa por `fd` (inicio del bloque), no por (fd, td),
    porque el `td` puede haberse retrocedido por un festivo y no coincidiría
    con el planificado.

    on_progress(i, n, fd, td)     -> qué bloque de la rejilla va (como antes).
    on_poll_progress(i, n, fase)  -> ver FlexClient.fetch(): dentro de CADA
                                     bloque, en qué intento de sondeo a IBKR
                                     va. Sin esto, un bloque grande puede
                                     estar varios MINUTOS sin mandar nada —
                                     visto en la práctica, tiempo de sobra
                                     para que Streamlit Cloud (detrás de un
                                     proxy) dé la conexión del navegador por
                                     muerta aunque el proceso siga vivo aquí
                                     y termine guardando bien (pedido por
                                     Juan: "recargué la página y los datos
                                     estaban", justo ese síntoma).
    """
    # El corte final es el ÚLTIMO CIERRE DISPONIBLE, no hoy: pedir hasta hoy
    # cuando IBKR aún no ha generado el cierre da 1003 (o un 1001 transitorio en
    # bloques grandes). Se descubre igual que en el incremental.
    if end is None:
        end = (client.latest_available_date(on_progress=on_poll_progress)
               or dt.datetime.now().strftime("%Y%m%d"))
    state.history_start = state.history_start or start

    # La rejilla de ventanas se planifica SIEMPRE desde `state.history_start`
    # (fijado una única vez), nunca desde el `start` de esta llamada. Si se
    # usara `start` aquí, dos backfills con una fecha de inicio ligeramente
    # distinta (p. ej. 01/01 vs 06/01) generan rejillas desplazadas que no
    # coinciden por `fd` con las ya hechas: `pending` las ve como nuevas y
    # descarga TODO el histórico otra vez, duplicado y solapado con lo que ya
    # había. La reanudación exige una rejilla estable entre llamadas.
    done = {w[0] for w in state.windows_done}

    windows = plan_windows(state.history_start, end, lead_days=LEAD_DAYS)
    pending = [w for w in windows if w[0] not in done]
    log.info("Backfill: %d ventanas, %d pendientes", len(windows), len(pending))

    for i, (fd, td) in enumerate(pending, 1):
        if on_progress:
            on_progress(i, len(pending), fd, td)
        try:
            td_real, path = _fetch_retreating(client, fd, td, on_poll_progress=on_poll_progress)
        except TokenExpired as e:
            state.paused_reason = str(e)
            state.save(state_path)
            raise
        except FlexError as e:
            state.last_error = str(e)
            state.save(state_path)
            raise
        state.windows_done.append([fd, td_real, path])
        state.last_error = ""
        state.save(state_path)          # guardar tras CADA ventana

    state.last_run = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    state.save(state_path)
    return state


# --------------------------------------------------------------------------
# Incremental diario
# --------------------------------------------------------------------------

def incremental(client: FlexClient, state: SyncState, state_path: str,
                overlap: int = OVERLAP_DAYS) -> dict:
    """Sincronización diaria. Devuelve un informe del resultado.

    Solapa `overlap` días hacia atrás porque IBKR revisa extractos ya emitidos:
    dividendos reclasificados, comisiones ajustadas, retenciones corregidas.
    Sin solape esas correcciones no llegan nunca.

    El `td` NO es hoy: se descubre primero el último cierre generado por IBKR y
    se pide sólo hasta él. Pedir hasta hoy cuando el cierre aún no está genera
    1003 para todo el bloque (verificado contra el servicio real), no un
    truncado silencioso como asumía §19.4. Que no haya datos nuevos —fin de
    semana, festivo, ejecución antes del proceso nocturno— es `no_new_data`,
    nunca un error.
    """
    if state.paused_reason:
        return dict(status="paused", reason=state.paused_reason)

    if not state.watermark:
        return dict(status="error", reason="Sin watermark: falta el backfill inicial")

    # 1) Descubrir el último cierre disponible (query sin fd/td).
    try:
        latest = client.latest_available_date()
    except TokenExpired as e:
        state.paused_reason = str(e)
        state.save(state_path)
        return dict(status="paused", reason=str(e))
    except RateLimitBlocked as e:
        # 1025: reintentar lo realimenta. No es error del motor; esperar y reintentar.
        state.last_error = str(e)
        state.save(state_path)
        return dict(status="blocked", reason=str(e))
    except FlexError as e:
        state.last_error = str(e)
        state.save(state_path)
        return dict(status="retry", reason=str(e))

    if not latest or latest <= state.watermark:
        return dict(status="no_new_data", watermark=state.watermark, latest=latest)

    # Guarda: tras una parada larga el hueco puede superar un bloque; un solo
    # incremental no basta y hay que reconstruir con un backfill multi-bloque.
    fd = (dt.datetime.strptime(state.watermark, "%Y%m%d")
          - dt.timedelta(days=overlap)).strftime("%Y%m%d")
    if (dt.datetime.strptime(latest, "%Y%m%d")
            - dt.datetime.strptime(fd, "%Y%m%d")).days > MAX_WINDOW_DAYS:
        return dict(status="gap_too_large", watermark=state.watermark, latest=latest,
                    reason=f"Hueco {fd}→{latest} > {MAX_WINDOW_DAYS} días: ejecuta un backfill")

    # 2) Pedir el bloque watermark−solape → último cierre disponible.
    try:
        path = client.fetch(fd, latest)
    except StatementNotAvailable:
        # El cierre se retiró entre el descubrimiento y la descarga: sin datos.
        return dict(status="no_new_data", watermark=state.watermark)
    except TokenExpired as e:
        state.paused_reason = str(e)
        state.save(state_path)
        return dict(status="paused", reason=str(e))
    except RateLimitBlocked as e:
        state.last_error = str(e)
        state.save(state_path)
        return dict(status="blocked", reason=str(e))
    except FlexError as e:
        state.last_error = str(e)
        state.save(state_path)
        return dict(status="retry", reason=str(e))

    state.last_run = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    state.last_error = ""
    state.save(state_path)
    return dict(status="ok", raw_path=path, requested=(fd, latest))


# --------------------------------------------------------------------------
# Post-proceso: watermark, conflictos y canario de regresión
# --------------------------------------------------------------------------

def update_watermark(state: SyncState, state_path: str, ingested_dates: list[str]):
    """El watermark sale de los DATOS, no de la petición.

    Si pides hasta hoy y IBKR aún no ha generado el cierre de hoy, devuelve
    hasta ayer sin avisar. Guardar el `td` pedido dejaría un hueco permanente
    que el solape del día siguiente no cubriría.
    """
    if not ingested_dates:
        return
    newest = max(ingested_dates)
    if newest > state.watermark:
        log.info("Watermark %s -> %s", state.watermark or "(vacío)", newest)
        state.watermark = newest
        state.save(state_path)


def check_nav_conflicts(existing: dict[str, float], incoming: dict[str, float],
                        tol: float = 1e-6) -> list[tuple[str, float, float]]:
    """Compara el NAV de las fechas que se repiten entre bloques.

    En los 945 días validados el resultado fue 0 conflictos. Un conflicto
    significa que IBKR ha reexpresado un dato histórico: hay que registrarlo
    y alertar, nunca sobrescribir en silencio.
    """
    return [(d, existing[d], incoming[d]) for d in set(existing) & set(incoming)
            if abs(existing[d] - incoming[d]) > tol]


def check_golden(state: SyncState, state_path: str, recomputed: dict[str, float],
                 tol_bp: float = 1.0) -> list[str]:
    """Canario de regresión que corre en cada sincronización.

    Los años cerrados son inmutables: 2023 dio +7,497979 % y no puede cambiar.
    Si se mueve, o IBKR ha reexpresado histórico o hay un bug en el motor. En
    ambos casos se quiere saber ese mismo día. Coste: cero.
    """
    if not state.golden:
        state.golden = {k: v for k, v in recomputed.items()}
        state.save(state_path)
        return []
    drift = []
    for year, ref in state.golden.items():
        now = recomputed.get(year)
        if now is None:
            continue
        if abs(now - ref) * 100 > tol_bp:
            drift.append(f"{year}: {ref:.6f} % -> {now:.6f} % "
                         f"({(now-ref)*100:+.2f} pb)")
    return drift


# --------------------------------------------------------------------------
# Tarea diaria completa
# --------------------------------------------------------------------------

def daily_job(client: FlexClient, state_path: str, query_id: str,
              parse_fn, recompute_fn, notify_fn=None, on_progress=None) -> dict:
    """Orquesta la sincronización diaria de una cuenta.

    parse_fn(raw_path)     -> (nav_por_fecha: dict, fechas: list[str])
    recompute_fn()         -> {"2023": 7.497979, ...} sobre TODO el histórico
    notify_fn(nivel, msg)  -> canal de aviso al usuario
    on_progress(i, n, msg) -> igual que el `on_progress` de backfill() (ver más
                              arriba), pero aquí con 3 pasos fijos en vez de un
                              bloque por ventana: pedir a IBKR, parsear lo
                              recibido, recalcular el histórico completo.
                              Sin esto, la interfaz se queda con un único
                              mensaje estático mientras dure la llamada — y la
                              espera a IBKR (que genera el informe de forma
                              asíncrona) puede ser la parte más larga de toda
                              la sincronización, justo la que menos aviso
                              tenía (pedido por Juan: "un contador para que el
                              usuario vea que sigue haciendo cosas").

    El recálculo es SIEMPRE completo. Añadir sólo el día nuevo al final de la
    cadena no vale: una corrección dentro de la ventana de solape cambia un
    retorno pasado y arrastra todo lo posterior. Recalcular 945 días son
    milisegundos.
    """
    def _tick(i, msg):
        if on_progress:
            on_progress(i, 3, msg)

    state = SyncState.load(state_path, query_id)
    _tick(1, "Preguntando a IBKR por el último cierre disponible…")
    res = incremental(client, state, state_path)

    if notify_fn:
        if res["status"] == "paused":            # token caducado: acción del usuario
            notify_fn("action_required", res["reason"])
        elif res["status"] == "gap_too_large":   # parada larga: hay que hacer backfill
            notify_fn("action_required", res["reason"])
        elif res["status"] == "blocked":         # 1025: esperar, no machacar
            notify_fn("info", res["reason"])
    if res["status"] != "ok":
        return res

    prev_watermark = state.watermark
    _tick(2, "Descarga recibida — parseando el extracto…")
    nav, dates = parse_fn(res["raw_path"])
    if not dates or max(dates) <= prev_watermark:
        log.info("Sin fechas nuevas (fin de semana, festivo o cierre no generado)")
        return dict(status="no_new_data", watermark=state.watermark)

    update_watermark(state, state_path, dates)
    _tick(3, "Recalculando el histórico completo…")
    drift = check_golden(state, state_path, recompute_fn())
    if drift and notify_fn:
        notify_fn("warning", "Los años cerrados han cambiado: " + " · ".join(drift))

    return dict(status="ok", watermark=state.watermark,
                new_dates=[d for d in dates if d > prev_watermark],
                golden_drift=drift)
