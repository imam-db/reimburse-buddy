"""A served upload survives a lost disk (ADR 0010 §5).

Uploads stay in `UPLOAD_DIR` because extraction, OCR and duplicate detection read them there, and with the
postgres backend the bytes are also stored in `uploaded_files`. When a host with an ephemeral disk drops
the file, the route brings it back before serving it — with the same bytes, the same content type and the
same ownership rules as before.
"""

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, claim_state, upload_flow

from conftest import install_test_users, login_as

TRIP = dict(purpose="Client visit", destination="Surabaya", start_date="2026-10-02", end_date="2026-10-04")
BYTES = b"%PDF-1.4 synthetic receipt"


class FileRows(claim_state.MemoryClaimRows):
    """Memory rows that keep the uploaded bytes, like `uploaded_files` does."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def save_file(self, stored_as, data, owner_id, content_type="", sha256=""):
        self.files[stored_as] = data

    def read_file(self, stored_as):
        return self.files.get(stored_as)


@pytest.fixture
def rows():
    fake = FileRows()
    claim_state.set_rows(fake)
    yield fake
    claim_state.set_rows(None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-001")
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def draft_with(stored="Ddraft01_abc_hotel.pdf", name="hotel.pdf", emp="EMP-001", did="draft01"):
    """A draft with one file on disk, as `POST /employee/drafts` leaves it."""
    web.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (web.UPLOAD_DIR / stored).write_bytes(BYTES)
    card = upload_flow._card_from_files([])
    card["files"] = [{"id": "f1", "name": name, "stored": stored, "extract": {"fields": {}, "error": None},
                      "sha256": "sha-of-" + stored, "doc_type": "hotel_receipt",
                      "doc_type_source": "ocr", "error": None}]
    d = {"id": did, "emp": emp, "header": dict(TRIP), "cards": [card], "errors": []}
    web.DRAFTS[did] = d
    return d


def url(did="draft01", fid="f1"):
    return f"/employee/drafts/{did}/files/{fid}"


def test_a_draft_file_is_served_from_disk_while_it_is_there(client, rows):
    draft_with()
    r = client.get(url())
    assert r.status_code == 200 and r.content == BYTES
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_a_draft_file_is_restored_when_the_disk_copy_is_gone(client, rows):
    d = draft_with()
    upload_flow._persist_file(d["cards"][0]["files"][0], "EMP-001")  # what create_draft does
    (web.UPLOAD_DIR / "Ddraft01_abc_hotel.pdf").unlink()
    assert not (web.UPLOAD_DIR / "Ddraft01_abc_hotel.pdf").exists()

    r = client.get(url())

    assert r.status_code == 200 and r.content == BYTES
    assert (web.UPLOAD_DIR / "Ddraft01_abc_hotel.pdf").is_file()  # back where extraction expects it


def test_a_file_the_database_and_the_disk_both_lost_is_a_404(client, rows):
    draft_with()
    (web.UPLOAD_DIR / "Ddraft01_abc_hotel.pdf").unlink()
    assert client.get(url()).status_code == 404


def test_another_employees_draft_file_is_a_404(client, rows, tmp_path):
    draft_with(emp="EMP-001")
    install_test_users()
    try:
        assert login_as(client, "EMP-003", "/employee").status_code == 303
        assert client.get(url()).status_code == 404
        assert login_as(client, "EMP-001", "/employee").status_code == 303
        assert client.get(url()).status_code == 200
    finally:
        auth.set_store(None)


def test_a_file_type_outside_the_allowlist_is_a_404(client, rows):
    draft_with(stored="Ddraft01_abc_notes.txt", name="notes.txt")
    assert client.get(url()).status_code == 404


def test_a_stored_name_that_climbs_out_of_the_upload_dir_is_a_404(client, rows, tmp_path):
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"not yours")
    draft_with(stored="../outside.pdf", name="outside.pdf")
    assert client.get(url()).status_code == 404
