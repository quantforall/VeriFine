"""
VeriFine — espejo entre el directorio de trabajo local de la sesión (un
scratch dir efímero) y la carpeta "VeriFine" en el Drive del usuario.

El resto del programa (`q4_sync.py`, `q4_license.py`, `q4_ingest.py`,
`q4_parser.py`, y la mayor parte de `app.py`) no sabe nada de Drive: sigue
leyendo/escribiendo un `RAW_DIR` local PLANO exactamente como hace hoy — un
solo directorio, sin subcarpetas, ni un solo cambio en esos módulos por lo
de abajo. Esta capa es la única que habla con `q4_drive.py`, y la única que
sabe que en Drive sí hay subcarpetas:

  1. Al empezar la sesión, `init_session_storage()` crea ese directorio local
     y, si hay Drive conectado, lo hidrata con lo que ya hubiera guardado.
  2. Cada vez que algo se escribe de forma durable (un XML nuevo, el estado
     de sincronización, la licencia, las credenciales de IBKR), `sync_up()`
     sube esos mismos ficheros a Drive.

Organización en Drive (a petición expresa, para que el usuario vea su
carpeta ordenada en vez de todo suelto):

    VeriFine/
    ├── XML/              — extractos crudos de IBKR (*.xml)
    ├── JSON/              — parseo cacheado de cada crudo (*.parsed.json)
    ├── license.json
    ├── state_<queryId>.json
    └── ibkr_credentials.json

`_target_subdir(name)` decide por SUFIJO del nombre a qué subcarpeta va cada
fichero — nunca por dónde vive localmente (ahí sigue todo plano, ver arriba).
Instalaciones de antes de esto tenían XML/JSON sueltos en la raíz;
`_migrate_loose_files_to_subfolders()` los mueve la primera vez que los
encuentra (una comprobación barata — un `list_files()` — en cualquier sesión
posterior que ya no tenga nada suelto).

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

XML_SUBDIR = "XML"
JSON_SUBDIR = "JSON"


def _target_subdir(name: str) -> str | None:
    """A qué subcarpeta de Drive pertenece `name` por su sufijo — `None`
    para la raíz (license.json, state_<queryId>.json, ibkr_credentials.json,
    candados). El `.parsed.json` va ANTES que un `.json` a secas no
    calificaría — mismo sufijo que usa `q4_parser._cache_path`."""
    if name.endswith(".parsed.json"):
        return JSON_SUBDIR
    if name.endswith(".xml"):
        return XML_SUBDIR
    return None


def _folder_for(drive: DriveFolder, name: str) -> DriveFolder:
    """La carpeta de Drive (raíz o subcarpeta) donde vive `name`."""
    sub = _target_subdir(name)
    return drive.subfolder(sub) if sub else drive


def _migrate_loose_files_to_subfolders(drive: DriveFolder) -> None:
    """Migración de una sola vez: XML/`.parsed.json` que quedaron sueltos en
    la raíz de antes de que existieran las subcarpetas — se mueven (subir a
    la subcarpeta correcta + borrar el suelto) la primera vez que se
    encuentran. Sesiones posteriores no tienen nada que migrar, y esta
    comprobación es casi gratis (un solo `list_files()` de la raíz)."""
    root_files = drive.list_files()
    loose = [(name, _target_subdir(name)) for name in root_files]
    loose = [(name, sub) for name, sub in loose if sub is not None]
    if not loose:
        return
    log.info("Migrando %d fichero(s) suelto(s) de la raíz de Drive a XML/JSON", len(loose))
    for name, sub in loose:
        content = drive.download(name)
        if content is None:
            continue
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        drive.subfolder(sub).upload(name, content, mime)
        drive.delete(name)


def _hydrate_folder(folder: DriveFolder, local_dir: str) -> int:
    """Descarga TODO lo que haya en `folder` (una carpeta o subcarpeta de
    Drive) a `local_dir`, en paralelo. Devuelve cuántos ficheros se
    escribieron. Un fichero que desaparezca a mitad se omite (no aborta la
    sesión); un token inválido (`AuthError`) sí se propaga — afecta a TODA
    la hidratación, no a un fichero suelto."""
    files = folder.list_files()
    written = 0

    def _fetch(name: str, file_id: str) -> tuple[str, bytes | None]:
        try:
            return name, folder.download_by_id(file_id)
        except AuthError:
            raise
        except DriveError:
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
    return written


def init_session_storage(drive: DriveFolder | None) -> str:
    """Directorio local para ESTA sesión, hidratado desde Drive si aplica.

    El directorio local queda PLANO (XML y JSON mezclados, como siempre) —
    la separación en subcarpetas es sólo del lado de Drive; ver cabecera
    del fichero. Devuelve la ruta a usar como `RAW_DIR` de la sesión."""
    local_dir = tempfile.mkdtemp(prefix="verifine_")
    if drive is None:
        return local_dir
    _migrate_loose_files_to_subfolders(drive)
    written = _hydrate_folder(drive, local_dir)
    written += _hydrate_folder(drive.subfolder(XML_SUBDIR), local_dir)
    written += _hydrate_folder(drive.subfolder(JSON_SUBDIR), local_dir)
    log.info("Sesión hidratada desde Drive: %d fichero(s)", written)
    return local_dir


def sync_up(drive: DriveFolder | None, raw_dir: str, *basenames: str) -> None:
    """Sube (crea o sobrescribe) cada nombre de `raw_dir` a la carpeta Drive
    que le corresponda por sufijo — XML/, JSON/ o la raíz (ver
    `_target_subdir`). No-op si `drive is None`. Un nombre que ya no exista
    localmente se salta sin más — no es un error, sólo algo que no había o
    que se decidió no persistir."""
    if drive is None:
        return
    for name in basenames:
        path = os.path.join(raw_dir, name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            content = fh.read()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        _folder_for(drive, name).upload(name, content, mime)


def sync_delete(drive: DriveFolder | None, *basenames: str) -> None:
    """Borra `basenames` de su carpeta Drive correspondiente. No-op si
    `drive is None`."""
    if drive is None:
        return
    for name in basenames:
        _folder_for(drive, name).delete(name)


def wipe_drive_folder(drive: DriveFolder | None) -> None:
    """Vacía el CONTENIDO de la carpeta Drive y sus subcarpetas XML/JSON
    (no las carpetas/puntero en sí) — equivalente Drive de
    `shutil.rmtree(RAW_DIR)` en `_danger_zone()`.

    Vacía primero XML/JSON y LUEGO la raíz (que incluye esas dos
    subcarpetas como entradas, ver `list_files`) — al revés, borrar la
    raíz primero recrearía "XML"/"JSON" vacías de inmediato en cuanto
    `drive.subfolder(...)` las buscara y no las encontrara."""
    if drive is None:
        return
    for sub in (XML_SUBDIR, JSON_SUBDIR):
        folder = drive.subfolder(sub)
        for name in list(folder.list_files()):
            folder.delete(name)
    for name in list(drive.list_files()):
        drive.delete(name)
