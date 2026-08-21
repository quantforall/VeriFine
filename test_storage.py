"""Tests de la capa de espejo local↔Drive (q4_storage.py), contra un doble
de DriveFolder — no hace falta red ni el propio q4_drive.py, sólo que cumpla
su interfaz pública (list_files/download/download_by_id/upload/delete/
subfolder)."""

from __future__ import annotations

import os

import pytest

import q4_storage as ST
from q4_drive import AuthError, DriveError


class FakeDriveFolder:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.upload_calls: list[str] = []
        self.delete_calls: list[str] = []
        self._subfolders: dict[str, "FakeDriveFolder"] = {}

    def list_files(self):
        return {name: {"id": name, "modifiedTime": ""} for name in self.files}

    def download(self, name):
        return self.files.get(name)

    def download_by_id(self, file_id):
        # En este doble el "id" es el propio nombre (ver list_files).
        return self.files[file_id]

    def upload(self, name, content, mime_type="application/octet-stream"):
        self.files[name] = content
        self.upload_calls.append(name)
        return name

    def delete(self, name):
        self.files.pop(name, None)
        self.delete_calls.append(name)

    def subfolder(self, name):
        # `type(self)()`, no `FakeDriveFolder()` a secas: los dobles que
        # subclasean esto para simular fallos (FlakyDriveFolder,
        # DeadTokenDriveFolder, más abajo) deben propagar ese
        # comportamiento a la subcarpeta también — si no, la migración
        # movería el fichero "problemático" a una subcarpeta normal y el
        # fallo que el test quiere probar nunca llegaría a dispararse.
        if name not in self._subfolders:
            self._subfolders[name] = type(self)()
        return self._subfolders[name]


def test_init_session_storage_without_drive_is_empty_local_dir():
    local = ST.init_session_storage(None)
    assert os.path.isdir(local)
    assert os.listdir(local) == []


def test_init_session_storage_hydrates_from_drive():
    """Ficheros ya organizados en sus subcarpetas (instalación nueva, sin
    nada suelto que migrar) — el directorio LOCAL sigue quedando plano."""
    drive = FakeDriveFolder()
    drive.files["state_Q1.json"] = b'{"watermark":"20260101"}'
    drive.subfolder("XML").files["a.xml"] = b"<xml/>"
    local = ST.init_session_storage(drive)
    assert set(os.listdir(local)) == {"state_Q1.json", "a.xml"}
    assert open(os.path.join(local, "a.xml"), "rb").read() == b"<xml/>"


def test_init_session_storage_migrates_loose_files_from_root():
    """Instalación de ANTES de que existieran las subcarpetas: XML/JSON
    sueltos en la raíz. Deben acabar en Drive dentro de XML/JSON (no en la
    raíz). En local, el `.parsed.json` migrado ya cubre el análisis (ver
    `parse_file_cached`), así que el `.xml` NO hace falta bajarlo — mismo
    comportamiento que test_init_session_storage_skips_xml_when_parsed_json_
    already_covers_it, aquí sólo se comprueba que la migración no lo cambia."""
    drive = FakeDriveFolder()
    drive.files["a.xml"] = b"<xml/>"
    drive.files["a.xml.parsed.json"] = b"{}"
    drive.files["state_Q1.json"] = b'{"watermark":"20260101"}'

    local = ST.init_session_storage(drive)

    assert set(os.listdir(local)) == {"a.xml.parsed.json", "state_Q1.json"}
    # migrado de verdad en Drive, no sólo "también visible" en local:
    assert "a.xml" not in drive.files
    assert "a.xml.parsed.json" not in drive.files
    assert "state_Q1.json" in drive.files              # esto SÍ es de la raíz, no se toca
    assert drive.subfolder("XML").files == {"a.xml": b"<xml/>"}
    assert drive.subfolder("JSON").files == {"a.xml.parsed.json": b"{}"}


def test_init_session_storage_skips_xml_when_parsed_json_already_covers_it():
    """El caso normal: histórico ya parseado en una sesión anterior. No hace
    falta bajar el XML crudo -- basta con el `.parsed.json` para que
    `parse_file_cached()` no vuelva a tocar el XML (ver su docstring)."""
    drive = FakeDriveFolder()
    drive.subfolder("XML").files["a.xml"] = b"<xml/>"
    drive.subfolder("JSON").files["a.xml.parsed.json"] = b'{"nav": {}}'
    local = ST.init_session_storage(drive)
    assert set(os.listdir(local)) == {"a.xml.parsed.json"}


def test_init_session_storage_downloads_xml_when_parsed_json_is_corrupt():
    """Si el `.parsed.json` hidratado resulta corrupto, SÍ se baja el XML
    correspondiente -- así `parse_file_cached()` puede reparsear en vez de
    reventar por falta del crudo."""
    drive = FakeDriveFolder()
    drive.subfolder("XML").files["a.xml"] = b"<xml/>"
    drive.subfolder("JSON").files["a.xml.parsed.json"] = b"{not valid json"
    local = ST.init_session_storage(drive)
    assert set(os.listdir(local)) == {"a.xml.parsed.json", "a.xml"}


def test_init_session_storage_downloads_xml_without_matching_parsed_json():
    """Un XML sin su `.parsed.json` (aún no parseado, o borrado) se sigue
    bajando siempre -- nada que pueda saltarse."""
    drive = FakeDriveFolder()
    drive.subfolder("XML").files["b.xml"] = b"<xml/>"
    local = ST.init_session_storage(drive)
    assert set(os.listdir(local)) == {"b.xml"}


def test_init_session_storage_migration_is_idempotent():
    """Una segunda sesión, con todo ya migrado, no debe re-migrar ni
    duplicar nada — sólo hidratar (barato: un list_files() por carpeta)."""
    drive = FakeDriveFolder()
    drive.files["a.xml"] = b"<xml/>"
    ST.init_session_storage(drive)          # 1ª sesión: migra
    ST.init_session_storage(drive)          # 2ª sesión: nada que migrar
    assert drive.subfolder("XML").files == {"a.xml": b"<xml/>"}
    assert drive.files == {}


def test_init_session_storage_hydrates_many_files_in_parallel():
    # No hay forma sencilla de observar "en paralelo" desde fuera sin
    # cronómetro (frágil en CI) — lo que sí se puede comprobar es que la
    # paralelización no cambia el resultado: con más ficheros que
    # MAX_PARALLEL_DOWNLOADS, todos llegan igual, completos y correctos.
    drive = FakeDriveFolder()
    n = ST.MAX_PARALLEL_DOWNLOADS * 3 + 1
    xml_folder = drive.subfolder("XML")
    for i in range(n):
        xml_folder.files[f"f{i}.xml"] = f"<xml id={i}/>".encode()
    local = ST.init_session_storage(drive)
    assert set(os.listdir(local)) == set(xml_folder.files)
    for i in range(n):
        content = open(os.path.join(local, f"f{i}.xml"), "rb").read()
        assert content == f"<xml id={i}/>".encode()


def test_init_session_storage_skips_file_that_disappears_mid_hydration():
    class FlakyDriveFolder(FakeDriveFolder):
        def download_by_id(self, file_id):
            if file_id == "gone.xml":
                raise DriveError("404: ya no existe")
            return super().download_by_id(file_id)

    drive = FlakyDriveFolder()
    drive.files["gone.xml"] = b"X"
    drive.files["ok.xml"] = b"OK"
    local = ST.init_session_storage(drive)
    assert set(os.listdir(local)) == {"ok.xml"}


def test_init_session_storage_propagates_auth_error():
    class DeadTokenDriveFolder(FakeDriveFolder):
        def download_by_id(self, file_id):
            raise AuthError("token revocado")

    drive = DeadTokenDriveFolder()
    drive.files["a.xml"] = b"A"
    with pytest.raises(AuthError):
        ST.init_session_storage(drive)


def test_sync_up_routes_xml_to_xml_subfolder(tmp_path):
    drive = FakeDriveFolder()
    raw_dir = str(tmp_path)
    with open(os.path.join(raw_dir, "a.xml"), "wb") as fh:
        fh.write(b"AAA")
    ST.sync_up(drive, raw_dir, "a.xml", "missing.xml")
    assert drive.subfolder("XML").files == {"a.xml": b"AAA"}
    assert drive.subfolder("XML").upload_calls == ["a.xml"]
    assert drive.files == {}                     # nada se queda en la raíz
    assert drive.upload_calls == []


def test_sync_up_routes_parsed_json_to_json_subfolder(tmp_path):
    drive = FakeDriveFolder()
    raw_dir = str(tmp_path)
    with open(os.path.join(raw_dir, "a.xml.parsed.json"), "wb") as fh:
        fh.write(b"{}")
    ST.sync_up(drive, raw_dir, "a.xml.parsed.json")
    assert drive.subfolder("JSON").files == {"a.xml.parsed.json": b"{}"}
    assert drive.files == {}


def test_sync_up_keeps_other_files_at_root(tmp_path):
    """license.json/state_*.json/credentials no son ni XML ni
    .parsed.json — siguen yendo a la raíz, como siempre."""
    drive = FakeDriveFolder()
    raw_dir = str(tmp_path)
    for name in ("license.json", "state_Q1.json", "ibkr_credentials.json"):
        with open(os.path.join(raw_dir, name), "wb") as fh:
            fh.write(b"{}")
    ST.sync_up(drive, raw_dir, "license.json", "state_Q1.json", "ibkr_credentials.json")
    assert set(drive.files) == {"license.json", "state_Q1.json", "ibkr_credentials.json"}
    assert drive.subfolder("XML").files == {}
    assert drive.subfolder("JSON").files == {}


def test_sync_up_is_noop_without_drive(tmp_path):
    raw_dir = str(tmp_path)
    with open(os.path.join(raw_dir, "a.xml"), "wb") as fh:
        fh.write(b"AAA")
    ST.sync_up(None, raw_dir, "a.xml")  # no debe explotar sin drive conectado


def test_sync_delete_removes_from_correct_folder():
    drive = FakeDriveFolder()
    drive.subfolder("XML").files["old.xml"] = b"X"
    ST.sync_delete(drive, "old.xml")
    assert "old.xml" not in drive.subfolder("XML").files


def test_sync_delete_noop_without_drive():
    ST.sync_delete(None, "old.xml")  # no debe explotar


def test_wipe_drive_folder_clears_root_and_subfolders():
    drive = FakeDriveFolder()
    drive.files.update({"state_Q1.json": b"{}"})
    drive.subfolder("XML").files.update({"a.xml": b"A"})
    drive.subfolder("JSON").files.update({"a.xml.parsed.json": b"{}"})
    ST.wipe_drive_folder(drive)
    assert drive.files == {}
    assert drive.subfolder("XML").files == {}
    assert drive.subfolder("JSON").files == {}


def test_wipe_drive_folder_noop_without_drive():
    ST.wipe_drive_folder(None)  # no debe explotar


# --------------------------------------------------------------------------
# known_raw_names() — regresión: sidebar_source()/has_data en app.py usaban
# glob.glob(*.xml) para saber "¿hay datos ya sincronizados?", y con el XML
# omitido a propósito cuando ya está parseado (ver init_session_storage),
# ese glob daba FALSO NEGATIVO — un usuario con histórico ya sincronizado
# veía sólo la pestaña Configuración al recargar la página.
# --------------------------------------------------------------------------

def test_known_raw_names_includes_xml_without_parsed_json(tmp_path):
    (tmp_path / "a.xml").write_bytes(b"<xml/>")
    assert ST.known_raw_names(str(tmp_path)) == ["a.xml"]


def test_known_raw_names_includes_xml_covered_only_by_parsed_json(tmp_path):
    """El caso que causaba el bug: el XML no está descargado, sólo su
    .parsed.json (omitido a propósito, ver _already_parsed_names)."""
    (tmp_path / "a.xml.parsed.json").write_text('{"nav": {}}')
    assert ST.known_raw_names(str(tmp_path)) == ["a.xml"]


def test_known_raw_names_deduplicates_when_both_present(tmp_path):
    (tmp_path / "a.xml").write_bytes(b"<xml/>")
    (tmp_path / "a.xml.parsed.json").write_text('{"nav": {}}')
    assert ST.known_raw_names(str(tmp_path)) == ["a.xml"]


def test_known_raw_names_empty_dir():
    assert ST.known_raw_names("") == []           # ni siquiera existe: no explota


def test_known_raw_names_ignores_unrelated_files(tmp_path):
    (tmp_path / "a.xml").write_bytes(b"<xml/>")
    (tmp_path / "state_Q1.json").write_text("{}")
    (tmp_path / "license.json").write_text("{}")
    assert ST.known_raw_names(str(tmp_path)) == ["a.xml"]
