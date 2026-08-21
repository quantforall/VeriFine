"""
VeriFine — control de acceso por suscripción (Substack), en dos niveles.

Substack no tiene API pública ni webhooks (comprobado): no hay forma de que
este programa le pregunte en tiempo real "¿sigue pagando este cliente?". El
control de acceso vive, por tanto, en dos sitios que ya existen y no cuestan
nada de infraestructura propia:

  1. Un post de pago en Substack ("Área de clientes VeriFine") donde se
     publica el código del ciclo actual — nivel FULL, sin límites.
  2. Un fichero JSON público, sin ningún dato de clientes, con los códigos
     vigentes de cada nivel (p. ej. un Gist de GitHub):

         {"full_codes": ["VF-2026-08", ...], "free_codes": ["VF-FREE"]}

     Se lee con un GET simple, sin autenticación ni servidor propio.
     `free_codes` es, en la práctica, un único código evergreen (`VF-FREE`)
     que se manda en el email de bienvenida automático de Substack a
     cualquiera que se suscriba gratis — no hay forma de generar un código
     distinto por suscriptor sin API de Substack, así que no se intenta.

La app guarda LOCALMENTE (junto a .ibkr_credentials) el último código
introducido, el nivel que autorizó (`last_tier`) y la fecha de la última
verificación con éxito. Si no hay conexión, sigue funcionando dentro de un
margen de gracia (GRACE_DAYS) desde esa fecha, al MISMO nivel que la última
vez; pasado ese margen, o si el código ya no está en ninguna lista vigente,
pide reintroducirlo.

Esto NO impide que alguien conserve una copia funcionando sin conexión para
siempre — sin servidor propio no hay forma de evitarlo. Lo que sí consigue es
que una copia que se conecta de vez en cuando (lo normal) deje de funcionar
en el margen de un par de ciclos tras cancelar.

Desactivado por defecto: si Q4_LICENSE_MANIFEST_URL no está configurada,
`fetch_valid_codes` devuelve None y `evaluate` autoriza cualquier código (o
ninguno) al nivel "full" — para no bloquear el desarrollo local ni los tests
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
    last_ok: str = ""    # "YYYY-MM-DD", fecha de la última verificación con éxito
    last_tier: str = ""  # "full" | "free" — nivel de esa última verificación con éxito

    @classmethod
    def load(cls, path: str) -> "LicenseState":
        if not os.path.exists(path):
            return cls()
        try:
            d = json.load(open(path))
            return cls(code=d.get("code", ""), last_ok=d.get("last_ok", ""),
                      last_tier=d.get("last_tier", ""))
        except (json.JSONDecodeError, OSError):
            return cls()

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        json.dump({"code": self.code, "last_ok": self.last_ok, "last_tier": self.last_tier},
                  open(path, "w"))

    def days_since_ok(self) -> int | None:
        if not self.last_ok:
            return None
        return (dt.date.today() - dt.date.fromisoformat(self.last_ok)).days


def fetch_valid_codes() -> dict[str, list[str]] | None:
    """`{"full": [...], "free": [...]}` del ciclo actual (y normalmente el
    anterior, para no cortar en seco justo el día de renovación), o None si
    no se pudo consultar: sin red, endpoint caído, o Q4_LICENSE_MANIFEST_URL
    sin configurar (licencia desactivada, ver docstring del módulo)."""
    if not MANIFEST_URL:
        return None
    try:
        r = requests.get(MANIFEST_URL, timeout=FETCH_TIMEOUT_S)
        r.raise_for_status()
        d = r.json()
        return {"full": list(d.get("full_codes", [])), "free": list(d.get("free_codes", []))}
    except (requests.RequestException, ValueError) as e:
        log.info(f"No se pudo verificar la licencia: {e}")
        return None


def evaluate(state: LicenseState,
            valid_codes: dict[str, list[str]] | None) -> tuple[bool, str, str, str]:
    """Decide si el código guardado autoriza el uso, y a qué nivel.

    Si autoriza por verificación online con éxito, actualiza
    `state.last_ok`/`state.last_tier` in-place (igual que `q4_sync.backfill`
    con `SyncState`); quien llame debe guardar `state` después si el
    resultado es autorizado.

    Devuelve (autorizado, tier, level, mensaje):
      - tier en {"full", "free", ""} — "" cuando no autorizado.
      - level en {"", "warning", "error"} para que la presentación decida
        cómo mostrarlo (una licencia en modo de gracia autoriza el uso pero
        merece avisar).
    """
    # Licencia desactivada (sin manifiesto configurado): no bloquea nada,
    # nivel "full" siempre — mismo escape de desarrollo/tests de siempre.
    if not MANIFEST_URL:
        return True, "full", "", ""

    if not state.code:
        return False, "", "error", "Introduce una licencia válida."

    if valid_codes is not None:
        # "full" primero: si por error un código apareciera en ambas listas,
        # gana el nivel más alto, nunca el más bajo.
        if state.code in valid_codes.get("full", []):
            state.last_ok = dt.date.today().isoformat()
            state.last_tier = "full"
            return True, "full", "", ""
        if state.code in valid_codes.get("free", []):
            state.last_ok = dt.date.today().isoformat()
            state.last_tier = "free"
            return True, "free", "", ""
        return False, "", "error", ("Ese código no es válido — consigue el tuyo en "
                                    "tu Substack (Área de clientes VeriFine).")

    # Sin conexión / endpoint caído: margen de gracia desde la última
    # verificación con éxito (no desde que se introdujo el código), AL MISMO
    # NIVEL de esa última verificación — sin `last_tier` (estado antiguo, o
    # nunca autorizado) no hay nivel al que dar el margen, así que bloquea.
    days = state.days_since_ok()
    if days is not None and days <= GRACE_DAYS and state.last_tier:
        return True, state.last_tier, "warning", (
            f"No se pudo verificar la licencia ahora mismo — funcionando en modo de "
            f"gracia ({GRACE_DAYS - days} día(s) restante(s)).")
    detail = " y el margen de gracia ha caducado" if days is not None else ""
    return False, "", "error", (f"No se pudo verificar la licencia{detail}. Conéctate a "
                                "internet y reintroduce tu código.")
