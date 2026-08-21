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
import json
import logging
import mimetypes
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import q4_probe as PR
from q4_drive import AuthError, DriveError, DriveFolder

log = logging.getLogger("q4.storage")

# Descargas simultáneas al hidratar una sesión nueva — I/O-bound (esperar la
# respuesta HTTP de Drive), no CPU, así que hilos bastan y no hace falta más
# concurrencia que ésta: con un histórico de decenas/cientos de crudos
# acumulados, bajarlos uno a uno en serie es la parte más lenta de abrir la
# app (varios segundos de solo esperar red antes de ver nada).
MAX_PARALLEL_DOWNLOADS = 8

# Techo de descargas Drive concurrentes por PROCESO, no sólo por sesión.
# MAX_PARALLEL_DOWNLOADS de arriba acota los hilos de UNA sesión, pero
# Streamlit Cloud no da un proceso por usuario — con muchas sesiones
# hidratando a la vez esos límites se SUMAN: 300 sesiones × 8 hilos serían
# hasta 2.400 descargas Drive simultáneas contra la cuota del mismo proyecto
# OAuth. Este semáforo es el techo real, compartido por todas las sesiones
# del proceso; 40 es un punto de partida conservador — Fase 0 (q4_probe) da
# los datos para ajustarlo con conocimiento real, no a ciegas.
GLOBAL_MAX_PARALLEL_DOWNLOADS = 40
_download_semaphore = threading.Semaphore(GLOBAL_MAX_PARALLEL_DOWNLOADS)

XML_SUBDIR = "XML"
JSON_SUBDIR = "JSON"
_PARSED_SUFFIX = ".parsed.json"


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


def _hydrate_folder(folder: DriveFolder, local_dir: str, files: dict[str, dict],
                    skip: frozenset[str] = frozenset()) -> int:
    """Descarga en paralelo los ficheros de `files` (ya listados por quien
    llama, ver `folder.list_files()` — así una lista se puede reutilizar
    para decidir qué saltar sin pedirla dos veces) a `local_dir`. `skip`:
    nombres que no hace falta bajar (ver `init_session_storage`, que salta
    el XML cuyo `.parsed.json` ya se hidrató y es válido). Devuelve cuántos
    ficheros se escribieron.

    Cada descarga pasa por `_download_semaphore` — acota cuántas de estas
    llamadas están en vuelo A LA VEZ EN TODO EL PROCESO, no sólo dentro de
    este `ThreadPoolExecutor` (ver su definición más arriba).

    Un fichero que desaparezca a mitad se omite (no aborta la sesión); un
    token inválido (`AuthError`) sí se propaga — afecta a TODA la
    hidratación, no a un fichero suelto."""
    targets = {name: info for name, info in files.items() if name not in skip}
    written = 0

    def _fetch(name: str, file_id: str) -> tuple[str, bytes | None]:
        try:
            with _download_semaphore:
                return name, folder.download_by_id(file_id)
        except AuthError:
            raise
        except DriveError:
            log.warning("No se pudo descargar %s de Drive; se omite esta sesión",
                       name, exc_info=True)
            return name, None

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_DOWNLOADS, len(targets)) or 1) as pool:
        futures = [pool.submit(_fetch, name, info["id"]) for name, info in targets.items()]
        for fut in as_completed(futures):
            name, content = fut.result()
            if content is None:
                continue
            with open(os.path.join(local_dir, name), "wb") as fh:
                fh.write(content)
            written += 1
    return written


def _already_parsed_names(json_files: dict[str, dict], local_dir: str) -> frozenset[str]:
    """Nombres de XML cuyo `.parsed.json` ya se hidrató en `local_dir` Y es
    JSON válido — esos XML no hace falta bajarlos (ver `init_session_storage`
    e `q4_parser.parse_file_cached`, que nunca vuelve a abrir el crudo si su
    caché ya existe). Se valida el contenido, no sólo que el nombre aparezca
    en el listado: si la caché resultara corrupta, el XML SÍ se baja (ver
    `test_init_session_storage_downloads_xml_when_parsed_json_is_corrupt`),
    para que `parse_file_cached` pueda reparsear en vez de reventar por
    falta del crudo."""
    out = set()
    for name in json_files:
        if not name.endswith(_PARSED_SUFFIX):
            continue
        local_path = os.path.join(local_dir, name)
        if not os.path.exists(local_path):
            continue  # no se pudo bajar (ver _hydrate_folder): nada que optimizar
        try:
            with open(local_path, encoding="utf-8") as fh:
                json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        out.add(name[: -len(_PARSED_SUFFIX)])
    return frozenset(out)


def init_session_storage(drive: DriveFolder | None, session_id: str | None = None) -> str:
    """Directorio local para ESTA sesión, hidratado desde Drive si aplica.

    El directorio local queda PLANO (XML y JSON mezclados, como siempre) —
    la separación en subcarpetas es sólo del lado de Drive; ver cabecera
    del fichero. Devuelve la ruta a usar como `RAW_DIR` de la sesión.

    `session_id` es puramente para correlacionar la métrica de duración
    (ver q4_probe.timed) entre sesiones en los logs — no cambia nada del
    comportamiento si se omite."""
    local_dir = tempfile.mkdtemp(prefix="verifine_")
    if drive is None:
        return local_dir
    with PR.timed("hydrate_session", session_id=session_id or "-"):
        _migrate_loose_files_to_subfolders(drive)

        written = _hydrate_folder(drive, local_dir, drive.list_files())

        json_folder = drive.subfolder(JSON_SUBDIR)
        json_files = json_folder.list_files()
        written += _hydrate_folder(json_folder, local_dir, json_files)

        # El XML es inmutable (q4_ingest.FlexClient.fetch) — si su
        # .parsed.json ya está aquí y es válido, bajarlo también sólo
        # alarga la hidratación sin que nada lo use (ver
        # _already_parsed_names).
        already_parsed = _already_parsed_names(json_files, local_dir)
        xml_folder = drive.subfolder(XML_SUBDIR)
        written += _hydrate_folder(xml_folder, local_dir, xml_folder.list_files(),
                                   skip=already_parsed)

        log.info("Sesión hidratada desde Drive: %d fichero(s) (%d XML omitidos, "
                 "ya parseados)", written, len(already_parsed))
    return local_dir


def known_raw_names(local_dir: str) -> list[str]:
    """Nombres (basenames, terminados en `.xml`) de los extractos
    "disponibles" en `local_dir` — incluye tanto los que están físicamente
    descargados como los que sólo tienen su `.parsed.json` (ver
    `init_session_storage`: el XML se salta a propósito cuando su
    `.parsed.json` ya cubre el análisis, `q4_parser.parse_file_cached` no
    necesita el crudo en ese caso).

    Usar esto en vez de `glob.glob(RAW_DIR + '/*.xml')` en cualquier sitio
    que quiera saber "¿hay datos ya sincronizados?" — ese glob por sí solo
    ahora da FALSOS NEGATIVOS para cualquier usuario que ya tuviera su
    histórico parseado en una sesión anterior (la mayoría, en producción):
    ve un RAW_DIR con `.parsed.json` pero sin los `.xml` correspondientes
    y concluye "no hay nada", cuando sí lo hay."""
    if not os.path.isdir(local_dir):
        return []
    names = set()
    for entry in os.listdir(local_dir):
        if entry.endswith(".xml"):
            names.add(entry)
        elif entry.endswith(_PARSED_SUFFIX):
            names.add(entry[: -len(_PARSED_SUFFIX)])
    return sorted(names)


def sync_up(drive: DriveFolder | None, raw_dir: str, *basenames: str) -> None:
    """Sube (crea o sobrescribe) cada nombre de `raw_dir` a la carpeta Drive
    que le corresponda por sufijo — XML/, JSON/ o la raíz (ver
    `_target_subdir`). No-op si `drive is None`. Un nombre que ya no exista
    localmente se salta sin más — no es un error, sólo algo que no había o
    que se decidió no persistir.

    Agrupado por carpeta destino antes de subir nada: `DriveFolder.upload()`
    ya evita resubir sin cambios con su propio `list_files()` interno, pero
    llamado UNA VEZ POR FICHERO, subir N ficheros nuevos a la misma carpeta
    (p. ej. un backfill grande) costaba N listados completos en vez de que
    los ficheros de la misma tanda compartan lo que ya se sabe de esa
    carpeta."""
    if drive is None:
        return
    for sub, names in _group_by_subdir(basenames).items():
        folder = drive.subfolder(sub) if sub else drive
        for name in names:
            path = os.path.join(raw_dir, name)
            if not os.path.exists(path):
                continue
            with open(path, "rb") as fh:
                content = fh.read()
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            folder.upload(name, content, mime)


def sync_delete(drive: DriveFolder | None, *basenames: str) -> None:
    """Borra `basenames` de su carpeta Drive correspondiente. No-op si
    `drive is None`. Agrupado por carpeta destino — mismo motivo que
    `sync_up`."""
    if drive is None:
        return
    for sub, names in _group_by_subdir(basenames).items():
        folder = drive.subfolder(sub) if sub else drive
        for name in names:
            folder.delete(name)


def _group_by_subdir(basenames: tuple[str, ...]) -> dict[str | None, list[str]]:
    out: dict[str | None, list[str]] = {}
    for name in basenames:
        out.setdefault(_target_subdir(name), []).append(name)
    return out


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
