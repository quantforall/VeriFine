"""Sondeo de alta contra el Flex Web Service real (§18.5).

Descarga un bloque CORTO y reciente para ver qué devuelve IBKR de verdad y
validar que la Flex Query trae las 14 secciones que el motor necesita.

El token es una credencial (rule 9 / §18.4): se lee de una variable de entorno
o de un fichero gitignoreado, NUNCA de la línea de comandos, y jamás se imprime.
La URL se redacta antes de cualquier log.

Uso:
    # opción A: fichero gitignoreado .ibkr_credentials (JSON)
    #   {"token": "...", "query_id": "..."}
    # opción B: variables de entorno
    #   export IBKR_FLEX_TOKEN=...   export IBKR_QUERY_ID=...
    python3 probe_ibkr.py [dias_atras]      # por defecto 10
"""

from __future__ import annotations

import os
import sys
import json
import logging
import datetime as dt

import q4_ingest as Q

logging.basicConfig(level=logging.INFO, format="  %(message)s")


def load_creds() -> tuple[str, str]:
    tok = os.environ.get("IBKR_FLEX_TOKEN")
    qid = os.environ.get("IBKR_QUERY_ID")
    if tok and qid:
        return tok.strip(), qid.strip()
    for path in (".ibkr_credentials", "ibkr_credentials.json"):
        if os.path.exists(path):
            d = json.load(open(path))
            return str(d["token"]).strip(), str(d["query_id"]).strip()
    sys.exit(
        "Sin credenciales. Crea .ibkr_credentials con {\"token\":\"...\","
        "\"query_id\":\"...\"} o exporta IBKR_FLEX_TOKEN e IBKR_QUERY_ID."
    )


def main() -> int:
    token, qid = load_creds()
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    today = dt.date.today()
    td = today.strftime("%Y%m%d")
    fd = (today - dt.timedelta(days=days)).strftime("%Y%m%d")

    print(f"Query {qid} · ventana corta {fd} → {td} (v=3)")
    client = Q.FlexClient(token=token, query_id=qid, raw_dir="./raw")

    try:
        path = client.fetch(fd, td)
    except Q.TokenExpired as e:
        print(f"\n[TOKEN] {e}")
        print("Es un error de usuario: regenera el token en Client Portal.")
        return 2
    except Q.FlexError as e:
        print(f"\n[FLEX {e.code}] {e}")
        return 3
    except Exception as e:                       # red, DNS, timeout…
        print(f"\n[RED] {type(e).__name__}: {e}")
        return 4

    xml = open(path, encoding="utf-8").read()
    print(f"\nDescargado {len(xml)/1024:.1f} KB → {path}")

    v = Q.validate_query(xml)
    print("\nvalidate_query:")
    for k, val in v.items():
        print(f"  {k:18} {val}")

    print("\n" + ("ALTA OK: la query trae todo y da posiciones diarias."
                  if v["ok"] else "REVISAR: " + (v["note"] or "ver arriba")))
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
