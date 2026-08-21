"""
VeriFine — Cliente REST para Google Drive (OAuth 2.0 + Drive API v3), sin SDKs.

Por qué a mano y no con `google-auth`/`google-api-python-client`: el resto del
proyecto ya habla con un servicio externo (IBKR Flex Web Service, ver
`q4_ingest.py`) con un cliente `requests` hecho a mano, y no hay nada aquí que
lo justifique menos — es la misma cantidad de protocolo (autorizar, refrescar,
subir/bajar ficheros) sin añadir dos dependencias pesadas.

Scopes usados — SOLO estos dos, a propósito:

    drive.file      acceso limitado a los ficheros/carpetas que ESTA app ha
                     creado. No permite buscar libremente por el Drive del
                     usuario.
    drive.appdata   una carpeta oculta y privada de la app dentro del Drive
                     del usuario, invisible en su interfaz normal.

Con `drive.file` no se puede "buscar la carpeta VeriFine por nombre" en
visitas futuras (no da acceso a listar todo el Drive) — por eso se guarda un
puntero con su ID en `appDataFolder` (ver `DriveFolder.resolve_or_create`).
Ambos scopes están, a fecha de diseño, fuera del proceso pesado de
verificación/auditoría de seguridad que exige Google para scopes "sensibles"
o "restringidos" — RE-CONFIRMAR esto contra la documentación oficial de
Google en el momento de desplegar, la clasificación de scopes puede cambiar.

SEGURIDAD: el token de acceso viaja en la cabecera Authorization, nunca en la
URL ni en logs — ver `_redact_tokens` antes de loguear cualquier objeto que
pueda contener uno.
"""

from __future__ import annotations

import json
import time
import random
import logging
import uuid
import datetime as dt
from dataclasses import dataclass, field
from urllib.parse import urlencode

import requests

import q4_probe as PR

log = logging.getLogger("q4.drive")

_rate_limited = PR.Counter("drive_rate_limited")

AUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.appdata",
]

FOLDER_MIME = "application/vnd.google-apps.folder"
FOLDER_NAME = "VeriFine"
POINTER_NAME = "verifine_pointer.json"
LOCK_STALE_S = 900          # mismo umbral que el .lock local de app.py hoy

REQUEST_TIMEOUT = 30

# Reintentos con backoff SOLO para 429 (cuota) y 5xx (Google caído/ocupado) —
# nunca para el resto de 4xx, que son errores reales (permiso, fichero
# borrado, etc.) y reintentarlos sólo tapa el síntoma. Antes cualquier
# >=400 lanzaba DriveError de inmediato: un pico de sesiones abriendo a la
# vez convertía un 429 pasajero en un error visible para el usuario en vez
# de una espera corta (ver diagnóstico de escalabilidad, Fase 1).
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 0.5


class DriveError(RuntimeError):
    pass


class AuthError(DriveError):
    """Token de Google rechazado, caducado sin refresh_token, o revocado por
    el usuario desde su cuenta — quien llame debe volver a pedir conexión,
    no reintentar sin más."""
    pass


@dataclass
class DriveTokens:
    access_token: str
    refresh_token: str
    expiry: str  # ISO 8601 UTC

    def expired(self, skew_seconds: int = 60) -> bool:
        try:
            exp = dt.datetime.fromisoformat(self.expiry)
        except ValueError:
            return True
        return dt.datetime.now(dt.timezone.utc) >= exp - dt.timedelta(seconds=skew_seconds)


# --------------------------------------------------------------------------
# Autorización
# --------------------------------------------------------------------------

def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """URL de consentimiento de Google.

    access_type=offline + prompt=consent: sin esto, un re-consentimiento
    posterior (p. ej. el usuario borró su sesión local) puede no traer
    refresh_token — Google solo lo emite de baja la primera vez que concede
    acceso a este client_id, salvo que se fuerce el consentimiento cada vez.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_BASE}?{urlencode(params)}"


def _tokens_from_response(d: dict, fallback_refresh: str = "") -> DriveTokens:
    expiry = (dt.datetime.now(dt.timezone.utc)
              + dt.timedelta(seconds=int(d.get("expires_in", 3600)))).isoformat()
    return DriveTokens(
        access_token=d["access_token"],
        refresh_token=d.get("refresh_token") or fallback_refresh,
        expiry=expiry,
    )


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str,
                   session: requests.Session | None = None) -> DriveTokens:
    session = session or requests.Session()
    r = session.request("POST", TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code": code,
    }, timeout=REQUEST_TIMEOUT)
    if r.status_code >= 400:
        raise AuthError(f"No se pudo canjear el código de Google: {r.text[:200]}")
    d = r.json()
    if "refresh_token" not in d:
        # Ver docstring de build_auth_url: puede pasar si el usuario ya había
        # concedido acceso antes. Sin refresh_token la sesión no sobrevive
        # más allá de la hora de vida del access_token — quien llame decide
        # si eso es aceptable (p. ej. reutilizar uno ya guardado en Drive).
        log.warning("Google no devolvió refresh_token en el intercambio de código")
    return _tokens_from_response(d)


def refresh_access_token(client_id: str, client_secret: str, tokens: DriveTokens,
                          session: requests.Session | None = None) -> DriveTokens:
    if not tokens.refresh_token:
        raise AuthError("Sin refresh_token: hay que reconectar con Google")
    session = session or requests.Session()
    r = session.request("POST", TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": tokens.refresh_token, "grant_type": "refresh_token",
    }, timeout=REQUEST_TIMEOUT)
    if r.status_code == 400:
        # invalid_grant: el usuario revocó el acceso desde su cuenta de Google.
        raise AuthError(f"Token de Google inválido o revocado: {r.text[:200]}")
    if r.status_code >= 400:
        raise DriveError(f"No se pudo refrescar el token de Google: {r.text[:200]}")
    return _tokens_from_response(r.json(), fallback_refresh=tokens.refresh_token)


def revoke(token: str, session: requests.Session | None = None) -> None:
    """Desconectar de verdad, no solo "olvidar" el token en el navegador:
    esto lo invalida en el lado de Google, así que deja de servir aunque
    alguien lo tuviera guardado en otro sitio. Pásale el refresh_token
    cuando lo haya — revocar un refresh_token invalida también todo access
    token emitido a partir de él; revocar solo el access_token no toca el
    refresh_token, que seguiría siendo válido.

    Tolerante a que Google ya no lo reconozca (400: el usuario ya lo había
    revocado él mismo desde su cuenta, o ya estaba caducado) — el
    resultado que importa, sin acceso, es el mismo; solo se considera
    fallo un error de verdad (5xx, red)."""
    if not token:
        return
    session = session or requests.Session()
    r = session.request("POST", REVOKE_URL, data={"token": token}, timeout=REQUEST_TIMEOUT)
    if 500 <= r.status_code < 600:
        raise DriveError(f"No se pudo revocar el token en Google: {r.text[:200]}")


def ensure_fresh(client_id: str, client_secret: str, tokens: DriveTokens,
                  session: requests.Session | None = None) -> DriveTokens:
    """Punto de entrada único para quien llame: devuelve `tokens` tal cual si
    el access_token sigue vivo, o uno refrescado si no — sin que quien llame
    tenga que saber de expiración."""
    if tokens.expired():
        return refresh_access_token(client_id, client_secret, tokens, session)
    return tokens


# --------------------------------------------------------------------------
# Carpeta "VeriFine" en el Drive del usuario
# --------------------------------------------------------------------------

@dataclass
class DriveFolder:
    """Una carpeta "VeriFine" ya localizada/creada en el Drive del usuario.

    No gestiona el refresco del access_token — quien construye/usa esta
    clase (típicamente `q4_storage.py`) debe pasarle un `DriveTokens` fresco
    (ver `ensure_fresh`) antes de cada sesión larga."""
    tokens: DriveTokens
    folder_id: str
    session: requests.Session = field(default_factory=requests.Session)
    # Cachés EN MEMORIA compartidas por todo el árbol de esta sesión —
    # `subfolder()` propaga el MISMO dict (no una copia) a cada hijo que
    # construye, así que lo que una llamada resuelve lo ven las demás sin
    # volver a pedirlo. Sólo dos claves, pobladas perezosamente:
    #   "subfolders": {(folder_id_padre, nombre): folder_id_hijo}
    #   "files":      {folder_id: {nombre: {"id":..., "modifiedTime":...}}}
    # Deliberadamente NO se usa para `list_files()` en sí (eso sigue yendo
    # siempre a red, ver más abajo) — sólo para el check-antes-de-escribir
    # de `upload()`/`delete()`, que ya se auto-invalida con cada escritura
    # propia. `acquire_lock()`/`release_lock()` tampoco la usan a propósito:
    # coordinan ENTRE sesiones, así que necesitan ver siempre el estado real
    # de Drive, no uno que otra sesión pudo dejar desactualizado aquí.
    _cache: dict = field(default_factory=dict)

    # -- transporte ---------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        headers = {"Authorization": f"Bearer {self.tokens.access_token}",
                   **kwargs.pop("headers", {})}
        delay = RETRY_BASE_DELAY_S
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            r = self.session.request(method, url, headers=headers,
                                     timeout=REQUEST_TIMEOUT, **kwargs)
            if r.status_code == 401:
                raise AuthError(f"Google rechazó el token: {r.text[:200]}")
            retryable = r.status_code == 429 or r.status_code >= 500
            if retryable and attempt < RETRY_ATTEMPTS:
                _rate_limited.hit(status=r.status_code, method=method)
                # Jitter (±25%) para que sesiones que chocaron con la misma
                # ventana de cuota no vuelvan a intentarlo todas en el mismo
                # instante — sin esto, el reintento sincronizado puede
                # recrear el mismo pico que lo causó.
                time.sleep(delay * random.uniform(0.75, 1.25))
                delay *= 2
                continue
            if r.status_code >= 400:
                raise DriveError(f"Drive API {method} {url} -> {r.status_code}: {r.text[:200]}")
            return r
        raise DriveError(f"Drive API {method} {url} -> {r.status_code} tras "
                         f"{RETRY_ATTEMPTS} intentos: {r.text[:200]}")

    def _upload_multipart(self, metadata: dict, content: bytes, mime_type: str) -> str:
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--".encode("utf-8")
        r = self._request("POST", f"{UPLOAD_API}/files",
                          params={"uploadType": "multipart"}, data=body,
                          headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return r.json()["id"]

    # -- localizar/crear la carpeta ------------------------------------------

    @classmethod
    def resolve_or_create(cls, tokens: DriveTokens,
                          session: requests.Session | None = None) -> "DriveFolder":
        """Busca el puntero en `appDataFolder`; si apunta a una carpeta que
        sigue existiendo, la usa. Si no hay puntero, o apunta a una carpeta
        borrada, crea la carpeta "VeriFine" (bajo `drive.file`, así que la
        propia app puede volver a encontrarla después) y guarda el puntero."""
        self = cls(tokens=tokens, folder_id="", session=session or requests.Session())
        pointer = self._read_appdata_pointer()
        folder_id = (pointer or {}).get("folder_id", "")
        if folder_id and self._folder_exists(folder_id):
            self.folder_id = folder_id
            return self
        if pointer:
            log.info("El puntero en appDataFolder apunta a una carpeta que ya no "
                     "existe; se crea una nueva")
        self.folder_id = self._create_folder()
        self._write_appdata_pointer(self.folder_id)
        return self

    def _folder_exists(self, folder_id: str) -> bool:
        r = self.session.request(
            "GET", f"{DRIVE_API}/files/{folder_id}",
            headers={"Authorization": f"Bearer {self.tokens.access_token}"},
            params={"fields": "id,trashed"}, timeout=REQUEST_TIMEOUT)
        if r.status_code == 404:
            return False
        if r.status_code >= 400:
            raise DriveError(f"No se pudo comprobar la carpeta: {r.text[:200]}")
        return not r.json().get("trashed", False)

    def _create_folder(self) -> str:
        r = self._request("POST", f"{DRIVE_API}/files",
                          json={"name": FOLDER_NAME, "mimeType": FOLDER_MIME})
        return r.json()["id"]

    def subfolder(self, name: str) -> "DriveFolder":
        """Localiza o crea una subcarpeta `name` DENTRO de esta carpeta —
        para VeriFine/XML y VeriFine/JSON (ver q4_storage.py, que decide qué
        va en cada una). Devuelve un `DriveFolder` nuevo apuntando a ella;
        list_files/upload/download/delete no cambian, sólo dependen de
        `folder_id` — así que funcionan igual sin más código.

        El `folder_id` resuelto SÍ se cachea en memoria (en `self._cache`,
        compartida con el hijo devuelto — ver el campo en la clase): dentro
        de una misma sesión, `subfolder("XML")` no cambia de un rerun de
        Streamlit a otra llamada — antes se re-resolvía (una búsqueda GET)
        en CADA llamada, aunque fuera la carpeta de siempre. La carpeta
        creada una vez no se borra sola, así que no hay riesgo de servir un
        id obsoleto salvo que alguien la borre a mano en Drive — ese caso ya
        no estaba cubierto tampoco antes de esto (no hay reintento de
        creación si `folder_id` deja de existir a media sesión)."""
        key = (self.folder_id, name)
        subfolders = self._cache.setdefault("subfolders", {})
        folder_id = subfolders.get(key)
        if folder_id is None:
            found = self._find_child_folder(name)
            folder_id = found["id"] if found else self._create_child_folder(name)
            subfolders[key] = folder_id
        return DriveFolder(tokens=self.tokens, folder_id=folder_id, session=self.session,
                           _cache=self._cache)

    def _find_child_folder(self, name: str) -> dict | None:
        r = self._request("GET", f"{DRIVE_API}/files", params={
            "q": (f"'{self.folder_id}' in parents and trashed=false and "
                 f"mimeType='{FOLDER_MIME}' and name='{name}'"),
            "fields": "files(id)",
        })
        files = r.json().get("files", [])
        return files[0] if files else None

    def _create_child_folder(self, name: str) -> str:
        r = self._request("POST", f"{DRIVE_API}/files",
                          json={"name": name, "mimeType": FOLDER_MIME,
                                "parents": [self.folder_id]})
        return r.json()["id"]

    def _find_in_appdata(self, name: str) -> dict | None:
        r = self._request("GET", f"{DRIVE_API}/files", params={
            "spaces": "appDataFolder",
            "q": f"name='{name}' and trashed=false",
            "fields": "files(id)",
        })
        files = r.json().get("files", [])
        return files[0] if files else None

    def _read_appdata_pointer(self) -> dict | None:
        found = self._find_in_appdata(POINTER_NAME)
        if not found:
            return None
        content = self._request("GET", f"{DRIVE_API}/files/{found['id']}",
                                params={"alt": "media"}).content
        try:
            return json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_appdata_pointer(self, folder_id: str) -> None:
        body = json.dumps({"folder_id": folder_id}).encode("utf-8")
        existing = self._find_in_appdata(POINTER_NAME)
        if existing:
            self._request("PATCH", f"{UPLOAD_API}/files/{existing['id']}",
                          params={"uploadType": "media"}, data=body,
                          headers={"Content-Type": "application/json"})
        else:
            self._upload_multipart({"name": POINTER_NAME, "parents": ["appDataFolder"]},
                                   body, "application/json")

    # -- contenido de la carpeta ----------------------------------------------

    def list_files(self) -> dict[str, dict]:
        """{name: {"id":..., "modifiedTime":...}} de lo que hay DENTRO de la
        carpeta VeriFine — plana, no hace falta recursión."""
        out: dict[str, dict] = {}
        page_token = None
        while True:
            params = {
                "q": f"'{self.folder_id}' in parents and trashed=false",
                "fields": "nextPageToken, files(id,name,modifiedTime)",
                "pageSize": 1000,
            }
            if page_token:
                params["pageToken"] = page_token
            d = self._request("GET", f"{DRIVE_API}/files", params=params).json()
            for f in d.get("files", []):
                out[f["name"]] = {"id": f["id"], "modifiedTime": f.get("modifiedTime", "")}
            page_token = d.get("nextPageToken")
            if not page_token:
                break
        return out

    def download(self, name: str) -> bytes | None:
        info = self.list_files().get(name)
        if not info:
            return None
        return self.download_by_id(info["id"])

    def download_by_id(self, file_id: str) -> bytes:
        """Como `download()`, pero sin volver a listar la carpeta antes —
        para cuando quien llama ya tiene el id de una llamada previa a
        `list_files()` (típicamente para bajar muchos ficheros a la vez:
        una lista + N descargas por id, en vez de N+1 listados)."""
        return self._request("GET", f"{DRIVE_API}/files/{file_id}",
                             params={"alt": "media"}).content

    def _known_files(self) -> dict[str, dict]:
        """Índice de esta carpeta, EN CACHÉ para `upload()`/`delete()` —
        el primero de los dos que se llame en la sesión hace el
        `list_files()` real; los siguientes (de esta carpeta o de otra
        `DriveFolder` que apunte al mismo `folder_id`, gracias a `_cache`
        compartida) lo reutilizan. Cada `upload()`/`delete()` propio
        actualiza esta misma entrada, así que nunca sirve un id que ELLOS
        mismos acaban de invalidar — ver el campo `_cache` en la clase para
        la salvedad de otra sesión escribiendo el mismo nombre por fuera."""
        by_folder = self._cache.setdefault("files", {})
        if self.folder_id not in by_folder:
            by_folder[self.folder_id] = self.list_files()
        return by_folder[self.folder_id]

    def upload(self, name: str, content: bytes,
              mime_type: str = "application/octet-stream") -> str:
        """Crea `name` en la carpeta si no existe, o sobrescribe su contenido
        si ya existe. Devuelve el fileId."""
        known = self._known_files()
        existing = known.get(name)
        if existing:
            self._request("PATCH", f"{UPLOAD_API}/files/{existing['id']}",
                          params={"uploadType": "media"}, data=content,
                          headers={"Content-Type": mime_type})
            return existing["id"]
        file_id = self._upload_multipart({"name": name, "parents": [self.folder_id]},
                                         content, mime_type)
        known[name] = {"id": file_id, "modifiedTime": ""}
        return file_id

    def delete(self, name: str) -> None:
        known = self._known_files()
        info = known.get(name)
        if info:
            self._request("DELETE", f"{DRIVE_API}/files/{info['id']}")
            known.pop(name, None)  # después de que la llamada tenga éxito —
                                    # si _request lanza, el índice se queda
                                    # como estaba, no como si ya se hubiera
                                    # borrado

    # -- cerrojo consultivo (mismo nivel de garantía que el .lock local) ------

    def acquire_lock(self, name: str, stale_seconds: int = LOCK_STALE_S) -> bool:
        """No es atómico — best-effort para el caso normal de una sesión a la
        vez, igual que el `.lock` de fichero local que sustituye. Si el
        cerrojo existe y es reciente, no se adquiere.

        La decisión de arriba usa `list_files()` SIN caché (siempre red,
        siempre el estado real de Drive — ver el campo `_cache` de la
        clase); sólo la escritura de abajo (`self.upload`) pasa por la
        caché de esta sesión, y sólo para decidir CREATE vs PATCH — el
        resultado final (el cerrojo queda escrito, con timestamp fresco) es
        el mismo en ambos casos."""
        info = self.list_files().get(name)
        if info:
            try:
                mtime = dt.datetime.fromisoformat(info["modifiedTime"].replace("Z", "+00:00"))
            except ValueError:
                mtime = dt.datetime.now(dt.timezone.utc)
            age = (dt.datetime.now(dt.timezone.utc) - mtime).total_seconds()
            if age < stale_seconds:
                return False
        self.upload(name, dt.datetime.now(dt.timezone.utc).isoformat().encode("utf-8"),
                   "text/plain")
        return True

    def release_lock(self, name: str) -> None:
        self.delete(name)
