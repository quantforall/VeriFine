"""Espera a que se despeje el rate limit y reanuda el backfill (gitignored)."""
import json, time, sys, logging
import q4_ingest as Q, q4_sync as S

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

WAIT = int(sys.argv[1]) if len(sys.argv) > 1 else 300
print(f"Esperando {WAIT}s a que se despeje el 1025 …", flush=True)
time.sleep(WAIT)

d = json.load(open(".ibkr_credentials"))
client = Q.FlexClient(token=str(d["token"]).strip(),
                      query_id=str(d["query_id"]).strip(), raw_dir="./raw")

# Sonda de despeje antes de arrancar el backfill.
try:
    latest = client.latest_available_date()
    print(f"Límite despejado · último cierre {latest}", flush=True)
except Q.FlexError as e:
    print(f"AÚN LIMITADO: [{e.code}] {e.message} — reintenta más tarde", flush=True)
    sys.exit(1)

STATE = "./raw/state.json"
state = S.SyncState.load(STATE, str(d["query_id"]).strip())


def prog(i, n, fd, td):
    print(f"  [{i}/{n}] descargando {fd} → {td} …", flush=True)


S.backfill(client, state, STATE, "20210106", end=latest, on_progress=prog)

print(f"\nBackfill completo: {len(state.windows_done)} ventanas", flush=True)
for fd, td, path in sorted(state.windows_done):
    print(f"   {fd} → {td}   {path.split('/')[-1]}", flush=True)
