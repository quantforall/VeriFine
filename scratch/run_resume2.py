"""Espera paciente al despeje del 1025 y reanuda el backfill (gitignored).

Prueba UNA sola petición cada `INTERVAL` s (hasta `MAX_PROBES` veces) para no
alimentar el contador de "too many failed attempts". En cuanto el token se
despeja, lanza el backfill reanudable.
"""
import json, time, sys, logging, datetime as dt
import q4_ingest as Q, q4_sync as S

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

INTERVAL = 600      # 10 min entre sondas
MAX_PROBES = 4      # ~40 min de margen

d = json.load(open(".ibkr_credentials"))
client = Q.FlexClient(token=str(d["token"]).strip(),
                      query_id=str(d["query_id"]).strip(), raw_dir="./raw")


def now():
    return dt.datetime.now().strftime("%H:%M:%S")


latest = None
for k in range(1, MAX_PROBES + 1):
    print(f"[{now()}] sonda {k}/{MAX_PROBES} …", flush=True)
    try:
        latest = client.latest_available_date()
        print(f"[{now()}] DESPEJADO · último cierre {latest}", flush=True)
        break
    except Q.FlexError as e:
        print(f"[{now()}] sigue limitado: [{e.code}] {e.message}", flush=True)
        if k < MAX_PROBES:
            time.sleep(INTERVAL)

if not latest:
    print("Sigue limitado tras todas las sondas. Reintentar más tarde.", flush=True)
    sys.exit(1)

STATE = "./raw/state.json"
state = S.SyncState.load(STATE, str(d["query_id"]).strip())


def prog(i, n, fd, td):
    print(f"[{now()}] [{i}/{n}] descargando {fd} → {td} …", flush=True)


S.backfill(client, state, STATE, "20210106", end=latest, on_progress=prog)

print(f"\n[{now()}] Backfill completo: {len(state.windows_done)} ventanas", flush=True)
for fd, td, path in sorted(state.windows_done):
    print(f"   {fd} → {td}   {path.split('/')[-1]}", flush=True)
