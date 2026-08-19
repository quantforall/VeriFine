"""
VeriFine — job diario (§19). Entrypoint que ejecuta launchd/cron una vez al día.

Encadena la sincronización incremental con IBKR, el recálculo completo y el
canario de regresión contra los golden de §13. Es idempotente: el solape de 10
días se deduplica por transactionID, así que ejecutarlo dos veces no rompe nada.

Credenciales: variables de entorno IBKR_FLEX_TOKEN / IBKR_QUERY_ID, o un fichero
gitignoreado .ibkr_credentials junto al proyecto. El token nunca se registra.

Almacén: Q4_RAW_DIR (por defecto ./raw). Un raw_dir distinto por cuenta aísla
varias cuentas sin que se pisen (cada una con su propio plist de launchd).

Salida: código 0 si ok/sin datos nuevos; !=0 si requiere atención (ver mapa al
final). Los avisos se registran en RAW_DIR/notifications.log y, en macOS, se
muestran como notificación del sistema.
"""

from __future__ import annotations

import os
import sys
import json
import glob
import logging
import datetime as dt

import q4_parser as P
import q4_engine as E
import q4_ingest as Q
import q4_sync as S

RAW_DIR = os.environ.get("Q4_RAW_DIR", "./raw")
STATE_PATH = os.path.join(RAW_DIR, "state.json")

os.makedirs(RAW_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(RAW_DIR, "daily.log")),
              logging.StreamHandler()])
log = logging.getLogger("q4.daily")


def load_creds() -> tuple[str, str]:
    tok, qid = os.environ.get("IBKR_FLEX_TOKEN"), os.environ.get("IBKR_QUERY_ID")
    if tok and qid:
        return tok.strip(), qid.strip()
    for p in (".ibkr_credentials", "ibkr_credentials.json"):
        if os.path.exists(p):
            d = json.load(open(p))
            return str(d["token"]).strip(), str(d["query_id"]).strip()
    sys.exit("Sin credenciales: define IBKR_FLEX_TOKEN/IBKR_QUERY_ID o crea .ibkr_credentials")


def all_raws() -> list[str]:
    return sorted(glob.glob(os.path.join(RAW_DIR, "*.xml")))


# --- callbacks que daily_job (§19) necesita --------------------------------

def parse_fn(raw_path: str):
    """(nav_por_fecha, fechas) del bloque recién descargado, para el watermark."""
    ds = P.load([raw_path])
    return P.nav_series(ds), ds.dates


def recompute_fn() -> dict[str, float]:
    """TWR EUR de cada año CERRADO sobre TODO el histórico (canario §19.6).

    El año en curso cambia a diario, así que se excluye. Los años cuyo tramo no
    es calculable (p. ej. el arranque en NAV=0 de 2021) se saltan.
    """
    ds = P.load(all_raws())
    dates = ds.dates
    this_year = dt.date.today().year
    out: dict[str, float] = {}
    for y in sorted({int(d[:4]) for d in dates}):
        if y >= this_year:
            continue
        prior = [d for d in dates if d < f"{y}0101"]
        in_year = [d for d in dates if d[:4] == str(y)]
        if not prior or not in_year:
            continue
        try:
            out[str(y)] = E.build_series(ds, prior[-1], in_year[-1]).total() * 100
        except Exception:
            continue
    return out


def notify_fn(level: str, msg: str):
    log.warning("NOTIFY[%s] %s", level, msg)
    with open(os.path.join(RAW_DIR, "notifications.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} [{level}] {msg}\n")
    if level in ("action_required", "warning") and sys.platform == "darwin":
        safe = msg.replace('"', "'")[:180]
        os.system(f"""osascript -e 'display notification "{safe}" with title "VeriFine"' 2>/dev/null""")


# --- orquestación ----------------------------------------------------------

def main() -> int:
    token, qid = load_creds()
    state = S.SyncState.load(STATE_PATH, qid)

    # Sembrar watermark y canario desde el backfill en la primera ejecución.
    if not state.watermark:
        raws = all_raws()
        if not raws:
            log.error("No hay crudos en %s: ejecuta primero el backfill", RAW_DIR)
            return 2
        state.watermark = P.load(raws).dates[-1]
        log.info("Watermark sembrado desde el backfill: %s", state.watermark)
    if not state.golden:
        # Auto-sembrado con los años cerrados de ESTA cuenta (no con los golden de
        # U7790974): así el canario detecta reexpresiones/bugs para cualquier
        # usuario, no sólo la cuenta de referencia.
        state.golden = {k: round(v, 6) for k, v in recompute_fn().items()}
        log.info("Canario auto-sembrado con los años cerrados de la cuenta: %s", state.golden)
    state.save(STATE_PATH)

    client = Q.FlexClient(token=token, query_id=qid, raw_dir=RAW_DIR)
    res = S.daily_job(client, STATE_PATH, qid, parse_fn, recompute_fn, notify_fn)
    log.info("Resultado: %s", {k: v for k, v in res.items() if k != "raw_path"})

    return {"ok": 0, "no_new_data": 0, "paused": 3, "blocked": 4,
            "gap_too_large": 5, "retry": 6, "error": 2}.get(res.get("status"), 1)


if __name__ == "__main__":
    raise SystemExit(main())
