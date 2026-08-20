"""Tests del cliente de Google Drive (q4_drive.py) con transporte simulado —
sin red, sin credenciales reales de Google. Mismo patrón que test_ingest.py:
un doble de `requests.Session` guionizado/con estado, sin mockear la librería
`requests` en sí."""

from __future__ import annotations

import json
import datetime as dt

import pytest

import q4_drive as D


# --------------------------------------------------------------------------
# Transporte simulado: un Drive en memoria lo bastante fiel para probar
# DriveFolder (list/get/create/update/delete, dos "espacios": la carpeta
# VeriFine y appDataFolder) sin hablar con Google de verdad.
# --------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status_code=200, json_body=None, content=b""):
        self.status_code = status_code
        self._json = json_body
        self.content = content
        self.text = content.decode("utf-8", "replace") if content else json.dumps(json_body or {})

    def json(self):
        return self._json if self._json is not None else json.loads(self.content)


class FakeDriveSession:
    """Simula lo justo de la API REST de Drive v3: un almacén en memoria de
    ficheros con id/name/parents/content/modifiedTime/trashed."""

    def __init__(self):
        self._files: dict[str, dict] = {}
        self._next_id = 1
        self.calls: list[tuple[str, str]] = []

    def _new_id(self) -> str:
        fid = f"f{self._next_id}"
        self._next_id += 1
        return fid

    def _now(self) -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    def request(self, method, url, headers=None, params=None, data=None,
               json=None, timeout=None):
        self.calls.append((method, url))
        params = params or {}

        if url.endswith("/files") and method == "GET":
            return self._list(params)
        if url.endswith("/files") and method == "POST":
            if params.get("uploadType") == "multipart":
                return self._create_from_multipart(data)
            return self._create_from_json(json or {})
        if "/files/" in url:
            fid = url.rsplit("/", 1)[-1]
            if method == "GET":
                if params.get("alt") == "media":
                    return self._download(fid)
                return self._get_meta(fid, params)
            if method == "PATCH":
                return self._update(fid, data)
            if method == "DELETE":
                return self._delete(fid)
        raise AssertionError(f"llamada no soportada por el fake: {method} {url}")

    def _list(self, params) -> FakeResp:
        q = params.get("q", "")
        space = params.get("spaces", "")
        out = []
        for fid, f in self._files.items():
            if f["trashed"]:
                continue
            if space == "appDataFolder" and "appDataFolder" not in f["parents"]:
                continue
            if space != "appDataFolder":
                # búsqueda dentro de una carpeta concreta: "'<id>' in parents"
                if "in parents" in q:
                    parent = q.split("'")[1]
                    if parent not in f["parents"]:
                        continue
            if "name='" in q:
                wanted = q.split("name='")[1].split("'")[0]
                if f["name"] != wanted:
                    continue
            out.append({"id": fid, "name": f["name"], "modifiedTime": f["modifiedTime"]})
        return FakeResp(200, {"files": out})

    def _create_from_json(self, meta: dict) -> FakeResp:
        fid = self._new_id()
        self._files[fid] = dict(name=meta.get("name", ""), parents=[],
                                content=b"", trashed=False, modifiedTime=self._now())
        return FakeResp(200, {"id": fid})

    def _create_from_multipart(self, body: bytes) -> FakeResp:
        # Formato fijo de _upload_multipart(): metadata JSON, luego el
        # contenido, separados por el boundary — suficiente con partir por
        # las líneas en blanco dobles que preceden a cada parte.
        text_boundary = body.split(b"\r\n", 1)[0]
        parts = body.split(text_boundary)
        meta_part = parts[1]
        content_part = parts[2]
        meta_json = meta_part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
        meta = json.loads(meta_json)
        content = content_part.split(b"\r\n\r\n", 1)[1]
        if content.endswith(b"\r\n"):
            content = content[:-2]
        fid = self._new_id()
        self._files[fid] = dict(name=meta.get("name", ""),
                                parents=meta.get("parents", []),
                                content=content, trashed=False, modifiedTime=self._now())
        return FakeResp(200, {"id": fid})

    def _get_meta(self, fid, params) -> FakeResp:
        f = self._files.get(fid)
        if not f:
            return FakeResp(404, {"error": "not found"})
        return FakeResp(200, {"id": fid, "trashed": f["trashed"]})

    def _download(self, fid) -> FakeResp:
        f = self._files.get(fid)
        if not f:
            return FakeResp(404, {"error": "not found"})
        return FakeResp(200, content=f["content"])

    def _update(self, fid, content: bytes) -> FakeResp:
        f = self._files.get(fid)
        if not f:
            return FakeResp(404, {"error": "not found"})
        f["content"] = content
        f["modifiedTime"] = self._now()
        return FakeResp(200, {"id": fid})

    def _delete(self, fid) -> FakeResp:
        f = self._files.get(fid)
        if f:
            f["trashed"] = True
        return FakeResp(204)


def make_folder(session=None) -> D.DriveFolder:
    session = session or FakeDriveSession()
    tokens = D.DriveTokens(access_token="AT", refresh_token="RT", expiry="2099-01-01T00:00:00+00:00")
    return D.DriveFolder.resolve_or_create(tokens, session=session)


# --------------------------------------------------------------------------
# Autorización
# --------------------------------------------------------------------------

def test_build_auth_url_has_offline_and_consent():
    url = D.build_auth_url("CID", "https://x/cb", "STATE123")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "client_id=CID" in url
    assert "state=STATE123" in url
    for scope in D.SCOPES:
        assert scope.split("/")[-1] in url  # el scope va urlencodeado en la query


class ScriptedTokenSession:
    """Igual que ScriptedSession de test_ingest.py, pero para los dos POST
    de intercambio/refresco de token (que usan session.request, no .get)."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, data=None, timeout=None):
        self.calls.append((method, url, data))
        status, body = self.script.pop(0)
        return FakeResp(status, body)


def test_exchange_code_returns_tokens_with_expiry():
    sess = ScriptedTokenSession([(200, {"access_token": "AT1", "refresh_token": "RT1",
                                        "expires_in": 3600})])
    tokens = D.exchange_code("CID", "SECRET", "https://x/cb", "CODE1", session=sess)
    assert tokens.access_token == "AT1" and tokens.refresh_token == "RT1"
    assert not tokens.expired()


def test_exchange_code_missing_refresh_token_falls_back_gracefully(caplog):
    sess = ScriptedTokenSession([(200, {"access_token": "AT1", "expires_in": 3600})])
    tokens = D.exchange_code("CID", "SECRET", "https://x/cb", "CODE1", session=sess)
    assert tokens.access_token == "AT1"
    assert tokens.refresh_token == ""


def test_exchange_code_error_raises_autherror():
    sess = ScriptedTokenSession([(400, {"error": "invalid_grant"})])
    with pytest.raises(D.AuthError):
        D.exchange_code("CID", "SECRET", "https://x/cb", "BADCODE", session=sess)


def test_refresh_access_token_keeps_old_refresh_if_not_reissued():
    old = D.DriveTokens(access_token="STALE", refresh_token="RT-ORIGINAL",
                        expiry="2000-01-01T00:00:00+00:00")
    sess = ScriptedTokenSession([(200, {"access_token": "AT2", "expires_in": 3600})])
    new = D.refresh_access_token("CID", "SECRET", old, session=sess)
    assert new.access_token == "AT2"
    assert new.refresh_token == "RT-ORIGINAL"


def test_refresh_access_token_revoked_raises_autherror():
    old = D.DriveTokens(access_token="STALE", refresh_token="RT",
                        expiry="2000-01-01T00:00:00+00:00")
    sess = ScriptedTokenSession([(400, {"error": "invalid_grant"})])
    with pytest.raises(D.AuthError):
        D.refresh_access_token("CID", "SECRET", old, session=sess)


def test_ensure_fresh_skips_refresh_when_still_valid():
    fresh = D.DriveTokens(access_token="AT", refresh_token="RT",
                          expiry=(dt.datetime.now(dt.timezone.utc)
                                  + dt.timedelta(hours=1)).isoformat())
    sess = ScriptedTokenSession([])  # no debería llamarse
    out = D.ensure_fresh("CID", "SECRET", fresh, session=sess)
    assert out is fresh


def test_revoke_noop_without_token():
    sess = ScriptedTokenSession([])  # no debería llamarse
    D.revoke("", session=sess)
    assert sess.calls == []


def test_revoke_success():
    sess = ScriptedTokenSession([(200, {})])
    D.revoke("SOME-TOKEN", session=sess)
    assert sess.calls[0][0] == "POST" and sess.calls[0][1] == D.REVOKE_URL


def test_revoke_tolerates_already_invalid_token():
    sess = ScriptedTokenSession([(400, {"error": "invalid_token"})])
    D.revoke("STALE-TOKEN", session=sess)  # no debe lanzar


def test_revoke_raises_on_server_error():
    sess = ScriptedTokenSession([(503, {"error": "unavailable"})])
    with pytest.raises(D.DriveError):
        D.revoke("SOME-TOKEN", session=sess)


def test_ensure_fresh_refreshes_when_expired():
    stale = D.DriveTokens(access_token="OLD", refresh_token="RT",
                          expiry="2000-01-01T00:00:00+00:00")
    sess = ScriptedTokenSession([(200, {"access_token": "NEW", "expires_in": 3600})])
    out = D.ensure_fresh("CID", "SECRET", stale, session=sess)
    assert out.access_token == "NEW"


# --------------------------------------------------------------------------
# Resolución/creación de la carpeta VeriFine
# --------------------------------------------------------------------------

def test_resolve_or_create_creates_folder_and_pointer_when_absent():
    session = FakeDriveSession()
    folder = make_folder(session)
    assert folder.folder_id
    # el puntero quedó en appDataFolder apuntando a esa carpeta
    pointer_files = [f for f in session._files.values() if f["name"] == D.POINTER_NAME]
    assert len(pointer_files) == 1
    assert json.loads(pointer_files[0]["content"])["folder_id"] == folder.folder_id


def test_resolve_or_create_reuses_existing_pointer():
    session = FakeDriveSession()
    first = make_folder(session)
    tokens = D.DriveTokens(access_token="AT", refresh_token="RT", expiry="2099-01-01T00:00:00+00:00")
    second = D.DriveFolder.resolve_or_create(tokens, session=session)
    assert second.folder_id == first.folder_id
    # no se creó una segunda carpeta "VeriFine"
    folders = [f for f in session._files.values() if f["name"] == D.FOLDER_NAME]
    assert len(folders) == 1


def test_resolve_or_create_recreates_when_pointed_folder_deleted():
    session = FakeDriveSession()
    first = make_folder(session)
    session._files[first.folder_id]["trashed"] = True
    tokens = D.DriveTokens(access_token="AT", refresh_token="RT", expiry="2099-01-01T00:00:00+00:00")
    second = D.DriveFolder.resolve_or_create(tokens, session=session)
    assert second.folder_id != first.folder_id


# --------------------------------------------------------------------------
# Contenido: subir, bajar, listar, borrar
# --------------------------------------------------------------------------

def test_upload_then_download_roundtrip():
    folder = make_folder()
    folder.upload("state_123.json", b'{"watermark": "20260101"}', "application/json")
    assert folder.download("state_123.json") == b'{"watermark": "20260101"}'


def test_upload_overwrites_existing_file_same_id():
    folder = make_folder()
    id1 = folder.upload("state_123.json", b"v1", "application/json")
    id2 = folder.upload("state_123.json", b"v2", "application/json")
    assert id1 == id2
    assert folder.download("state_123.json") == b"v2"


def test_download_missing_returns_none():
    folder = make_folder()
    assert folder.download("nope.xml") is None


def test_list_files_only_sees_folder_contents():
    folder = make_folder()
    folder.upload("a.xml", b"A")
    folder.upload("b.xml", b"B")
    names = set(folder.list_files())
    assert names == {"a.xml", "b.xml"}
    assert D.POINTER_NAME not in names  # eso vive en appDataFolder, espacio distinto


def test_delete_removes_from_listing():
    folder = make_folder()
    folder.upload("a.xml", b"A")
    folder.delete("a.xml")
    assert "a.xml" not in folder.list_files()
    assert folder.download("a.xml") is None


# --------------------------------------------------------------------------
# Cerrojo consultivo
# --------------------------------------------------------------------------

def test_acquire_lock_succeeds_when_absent():
    folder = make_folder()
    assert folder.acquire_lock("state_123.json.lock") is True


def test_acquire_lock_fails_when_fresh():
    folder = make_folder()
    folder.acquire_lock("state_123.json.lock")
    assert folder.acquire_lock("state_123.json.lock") is False


def test_acquire_lock_succeeds_when_stale():
    folder = make_folder()
    folder.acquire_lock("state_123.json.lock")
    info = folder.list_files()["state_123.json.lock"]
    # retrasamos artificialmente el modifiedTime para simular un cerrojo viejo
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=D.LOCK_STALE_S + 1))
    folder.session._files[info["id"]]["modifiedTime"] = old.isoformat().replace("+00:00", "Z")
    assert folder.acquire_lock("state_123.json.lock", stale_seconds=D.LOCK_STALE_S) is True


def test_release_lock_allows_immediate_reacquire():
    folder = make_folder()
    folder.acquire_lock("state_123.json.lock")
    folder.release_lock("state_123.json.lock")
    assert folder.acquire_lock("state_123.json.lock") is True
