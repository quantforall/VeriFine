"""Backfill real 2021→2026 contra IBKR (uso único, gitignored)."""
import json, logging, sys
import q4_ingest as Q, q4_sync as S

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

d = json.load(open(".ibkr_credentials"))
client = Q.FlexClient(token=str(d["token"]).strip(),
                      query_id=str(d["query_id"]).strip(), raw_dir="./raw")

latest = client.latest_available_date()
print(f"Último cierre disponible: {latest}", flush=True)

STATE = "./raw/state.json"
state = S.SyncState.load(STATE, str(d["query_id"]).strip())
state.history_start = "20210106"


def prog(i, n, fd, td):
    print(f"  [{i}/{n}] descargando {fd} → {td} …", flush=True)


S.backfill(client, state, STATE, "20210106", end=latest, on_progress=prog)

print(f"\nBackfill terminado: {len(state.windows_done)} ventanas", flush=True)
for fd, td, path in state.windows_done:
    print(f"   {fd} → {td}   {path.split('/')[-1]}", flush=True)
