"""
VeriFine — espejo entre el directorio de trabajo local de la sesión (un
scratch dir efímero) y la carpeta "VeriFine" en el Drive del usuario.

El resto del programa (`q4_sync.py`, `q4_license.py`, `q4_ingest.py`,
`q4_parser.py`, y la mayor parte de `app.py`) no sabe nada de Drive: sigue
leyendo/escribiendo un `RAW_DIR` local exactamente como hace hoy. Esta capa
es la única que habla con `q4_drive.py`:

  1. Al empezar la sesión, `init_session_storage()` crea ese directorio local
     y, si hay Drive conectado, lo hidrata con lo que ya hubiera guardado.
  2. Cada vez que algo se escribe de forma durable (un XML nuevo, el estado
     de sincronización, la licencia, las credenciales de IBKR), `sync_up()`
     sube esos mismos ficheros a Drive.

Si `drive` es `None` (desarrollo local con `Q4_STORAGE_BACKEND=local`, o
tests), todas las funciones de aquí son no-op salvo `init_session_storage`,
que simplemente da un directorio local vacío — el resto del programa sigue
funcionando exactamente igual que antes de que existiera esta capa.
"""

from __future__ import annotations

import os
import logging
import mimetypes
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from q4_drive import AuthError, DriveError, DriveFolder

log = logging.getLogger("q4.storage")

# Descargas simultáneas al hidratar una sesión nueva — I/O-bound (esperar la
# respuesta HTTP de Drive), no CPU, así que hilos bastan y no hace falta más
# concurrencia que ésta: con un histórico de decenas/cientos de crudos
# acumulados, bajarlos uno a uno en serie es la parte más lenta de abrir la
# app (varios segundos de solo esperar red antes de ver nada).
MAX_PARALLEL_DOWNLOADS = 8


def init_session_storage(drive: DriveFolder | None) -> str:
    """Directorio local para ESTA sesión, hidratado desde Drive si aplica.

    Devuelve la ruta a usar como `RAW_DIR` de la sesión."""
    local_dir = tempfile.mkdtemp(prefix="verifine_")
    if drive is None:
        return local_dir
    files = drive.list_files()   # una sola llamada: {name: {"id":..., ...}}
    written = 0

    def _fetch(name: str, file_id: str) -> tuple[str, bytes | None]:
        try:
            return name, drive.download_by_id(file_id)
        except AuthError:
            raise  # token inválido: afecta a TODA la hidratación, no solo a este fichero
        except DriveError:
            # p. ej. el fichero se borró entre el listado y la descarga —
            # antes `download(name)` volvía a listar y esto ya daba None sin
            # más; con el id ya en mano, el 404 llega como DriveError. Se
            # omite ese fichero, no se aborta la sesión entera por él.
            log.warning("No se pudo descargar %s de Drive; se omite esta sesión",
                       name, exc_info=True)
            return name, None

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_DOWNLOADS, len(files)) or 1) as pool:
        futures = [pool.submit(_fetch, name, info["id"]) for name, info in files.items()]
        for fut in as_completed(futures):
            name, content = fut.result()
            if content is None:
                continue
            with open(os.path.join(local_dir, name), "wb") as fh:
                fh.write(content)
            written += 1
    log.info("Sesión hidratada desde Drive: %d fichero(s)", written)
    return local_dir


def sync_up(drive: DriveFolder | None, raw_dir: str, *basenames: str) -> None:
    """Sube (crea o sobrescribe) cada nombre de `raw_dir` a la carpeta Drive.

    No-op si `drive is None`. Un nombre que ya no exista localmente se salta
    sin más — no es un error, sólo algo que no había o que se decidió no
    persistir."""
    if drive is None:
        return
    for name in basenames:
        path = os.path.join(raw_dir, name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            content = fh.read()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        drive.upload(name, content, mime)


def sync_delete(drive: DriveFolder | None, *basenames: str) -> None:
    """Borra `basenames` de la carpeta Drive. No-op si `drive is None`."""
    if drive is None:
        return
    for name in basenames:
        drive.delete(name)


def wipe_drive_folder(drive: DriveFolder | None) -> None:
    """Vacía el CONTENIDO de la carpeta Drive (no la carpeta/puntero en sí)
    — equivalente Drive de `shutil.rmtree(RAW_DIR)` en `_danger_zone()`."""
    if drive is None:
        return
    for name in list(drive.list_files()):
        drive.delete(name)
