"""Tests de la capa de espejo local↔Drive (q4_storage.py), contra un doble
de DriveFolder — no hace falta red ni el propio q4_drive.py, sólo que cumpla
su interfaz pública (list_files/download/upload/delete)."""

from __future__ import annotations

import os

import q4_storage as ST


class FakeDriveFolder:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.upload_calls: list[str] = []
        self.delete_calls: list[str] = []

    def list_files(self):
        return {name: {"id": name, "modifiedTime": ""} for name in self.files}

    def download(self, name):
        return self.files.get(name)

    def upload(self, name, content, mime_type="application/octet-stream"):
        self.files[name] = content
        self.upload_calls.append(name)
        return name

    def delete(self, name):
        self.files.pop(name, None)
        self.delete_calls.append(name)


def test_init_session_storage_without_drive_is_empty_local_dir():
    local = ST.init_session_storage(None)
    assert os.path.isdir(local)
    assert os.listdir(local) == []


def test_init_session_storage_hydrates_from_drive():
    drive = FakeDriveFolder()
    drive.files["state_Q1.json"] = b'{"watermark":"20260101"}'
    drive.files["a.xml"] = b"<xml/>"
    local = ST.init_session_storage(drive)
    assert set(os.listdir(local)) == {"state_Q1.json", "a.xml"}
    assert open(os.path.join(local, "a.xml"), "rb").read() == b"<xml/>"


def test_sync_up_uploads_existing_files_only(tmp_path):
    drive = FakeDriveFolder()
    raw_dir = str(tmp_path)
    with open(os.path.join(raw_dir, "a.xml"), "wb") as fh:
        fh.write(b"AAA")
    ST.sync_up(drive, raw_dir, "a.xml", "missing.xml")
    assert drive.files == {"a.xml": b"AAA"}
    assert drive.upload_calls == ["a.xml"]


def test_sync_up_is_noop_without_drive(tmp_path):
    raw_dir = str(tmp_path)
    with open(os.path.join(raw_dir, "a.xml"), "wb") as fh:
        fh.write(b"AAA")
    ST.sync_up(None, raw_dir, "a.xml")  # no debe explotar sin drive conectado


def test_sync_delete_removes_from_drive():
    drive = FakeDriveFolder()
    drive.files["old.xml"] = b"X"
    ST.sync_delete(drive, "old.xml")
    assert "old.xml" not in drive.files


def test_sync_delete_noop_without_drive():
    ST.sync_delete(None, "old.xml")  # no debe explotar


def test_wipe_drive_folder_clears_everything():
    drive = FakeDriveFolder()
    drive.files.update({"a.xml": b"A", "state_Q1.json": b"{}"})
    ST.wipe_drive_folder(drive)
    assert drive.files == {}


def test_wipe_drive_folder_noop_without_drive():
    ST.wipe_drive_folder(None)  # no debe explotar
