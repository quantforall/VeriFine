"""
VeriFine — control de acceso por suscripción (Substack).

Substack no tiene API pública ni webhooks (comprobado): no hay forma de que
este programa le pregunte en tiempo real "¿sigue pagando este cliente?". El
control de acceso vive, por tanto, en dos sitios que ya existen y no cuestan
nada de infraestructura propia:

  1. Un post de pago en Substack ("Área de clientes VeriFine") donde se
     publica el código del ciclo actual. El muro de pago lo hace Substack,
     no este programa — solo lo ve quien sigue suscrito.
  2. Un fichero JSON público, sin ningún dato de clientes, con los códigos
     vigentes (p. ej. un Gist de GitHub: {"valid_codes": ["VF-2026-08", ...]}).
     Se lee con un GET simple, sin autenticación ni servidor propio.

La app guarda LOCALMENTE (junto a .ibkr_credentials) el último código
introducido y la fecha de la última verificación con éxito. Si no hay
conexión, sigue funcionando dentro de un margen de gracia (GRACE_DAYS) desde
esa fecha; pasado ese margen, o si el código ya no está en la lista de
vigentes, pide reintroducirlo.

Esto NO impide que alguien conserve una copia funcionando sin conexión para
siempre — sin servidor propio no hay forma de evitarlo. Lo que sí consigue es
que una copia que se conecta de vez en cuando (lo normal) deje de funcionar
en el margen de un par de ciclos tras cancelar.

Desactivado por defecto: si Q4_LICENSE_MANIFEST_URL no está configurada,
`fetch_valid_codes` devuelve None y `evaluate` trata cualquier código (o
ninguno) como autorizado — para no bloquear el desarrollo local ni los tests
antes de tener el Gist y el post de Substack montados.
"""

from __future__ import annotations

import os
import json
import logging
import datetime as dt
from dataclasses import dataclass

import requests

log = logging.getLogger("q4.license")

MANIFEST_URL = os.environ.get("Q4_LICENSE_MANIFEST_URL", "")
GRACE_DAYS = 14      # días de uso offline tolerados desde la última verificación con éxito
FETCH_TIMEOUT_S = 5  # no bloquear el arranque si el endpoint no responde


@dataclass
class LicenseState:
    code: str = ""
    last_ok: str = ""  # "YYYY-MM-DD", fecha de la última verificación con éxito

    @classmethod
    def load(cls, path: str) -> "LicenseState":
        if not os.path.exists(path):
            return cls()
        try:
            d = json.load(open(path))
            return cls(code=d.get("code", ""), last_ok=d.get("last_ok", ""))
        except (json.JSONDecodeError, OSError):
            return cls()

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        json.dump({"code": self.code, "last_ok": self.last_ok}, open(path, "w"))

    def days_since_ok(self) -> int | None:
        if not self.last_ok:
            return None
        return (dt.date.today() - dt.date.fromisoformat(self.last_ok)).days


def fetch_valid_codes() -> list[str] | None:
    """Códigos vigentes del ciclo actual (y normalmente el anterior, para no
    cortar en seco justo el día de renovación), o None si no se pudo
    consultar: sin red, endpoint caído, o Q4_LICENSE_MANIFEST_URL sin
    configurar (licencia desactivada, ver docstring del módulo)."""
    if not MANIFEST_URL:
        return None
    try:
        r = requests.get(MANIFEST_URL, timeout=FETCH_TIMEOUT_S)
        r.raise_for_status()
        return list(r.json().get("valid_codes", []))
    except (requests.RequestException, ValueError) as e:
        log.info(f"No se pudo verificar la licencia: {e}")
        return None


def evaluate(state: LicenseState, valid_codes: list[str] | None) -> tuple[bool, str, str]:
    """Decide si el código guardado autoriza el uso.

    Si autoriza por verificación online con éxito, actualiza
    `state.last_ok` in-place (igual que `q4_sync.backfill` con `SyncState`);
    quien llame debe guardar `state` después si el resultado es autorizado.

    Devuelve (autorizado, nivel, mensaje) con nivel en
    {"", "warning", "error"} para que la presentación decida cómo mostrarlo
    (una licencia en modo de gracia autoriza el uso pero merece avisar).
    """
    # Licencia desactivada (sin manifiesto configurado): no bloquea nada,
    # pase lo que pase con el código guardado.
    if not MANIFEST_URL:
        return True, "", ""

    if not state.code:
        return False, "error", "Introduce el código de este mes (lo tienes en tu Substack)."

    if valid_codes is not None:
        if state.code in valid_codes:
            state.last_ok = dt.date.today().isoformat()
            return True, "", ""
        return False, "error", ("Ese código ya no es válido — coge el de este mes en "
                                 "tu Substack (Área de clientes VeriFine).")

    # Sin conexión / endpoint caído: margen de gracia desde la última
    # verificación con éxito (no desde que se introdujo el código).
    days = state.days_since_ok()
    if days is not None and days <= GRACE_DAYS:
        return True, "warning", (f"No se pudo verificar la licencia ahora mismo — "
                                  f"funcionando en modo de gracia "
                                  f"({GRACE_DAYS - days} día(s) restante(s)).")
    detail = " y el margen de gracia ha caducado" if days is not None else ""
    return False, "error", (f"No se pudo verificar la licencia{detail}. Conéctate a "
                             "internet y reintroduce el código de este mes.")
