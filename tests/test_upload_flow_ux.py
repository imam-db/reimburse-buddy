"""ADR 0007: the new-claim flow copy, feedback, card layout and file previews.

Every test uses a stub extractor, so none of them depends on Tesseract.
"""

from __future__ import annotations

import html
import re

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import upload_flow
from reimburse_web.extract import Draft, Field

TRIP = dict(purpose="Client visit", destination="Surabaya", start_date="2026-10-02", end_date="2026-10-04")
BAD_ERROR = "Could not read this file (TesseractNotFoundError); fill in the card by hand."
FIELDS = ("doc_type", "date", "amount", "merchant", "flight_no", "nights", "passenger")


def fields(**values) -> dict:
    out = {k: Field() for k in FIELDS}
    for k, v in values.items():
        out[k] = Field(v, 0.9, "ocr")
    return out


def good(**values) -> Draft:
    return Draft(fields=fields(**values))


def bad() -> Draft:
    d = Draft(fields=fields(doc_type="other"))
    d.error = BAD_ERROR
    return d


class StubExtractor:
    name = "stub"

    def __init__(self, mapping: dict):
        self.mapping = mapping

    def extract(self, path):
        for name, draft in self.mapping.items():
            if name in path.name:
                return draft() if callable(draft) else draft
        return Draft(error="no stub for " + path.name)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-001")
    monkeypatch.delenv("EMPLOYEE_QA", raising=False)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def stubbed(monkeypatch, mapping: dict) -> None:
    monkeypatch.setattr(upload_flow, "get_extractor", lambda: StubExtractor(mapping))


def drop(client, monkeypatch, mapping: dict) -> dict:
    stubbed(monkeypatch, mapping)
    files = [("files", (name, b"\x89PNG\r\n\x1a\n")) for name in mapping]
    r = client.post("/employee/drafts", data=TRIP, files=files, follow_redirects=False)
    assert r.status_code == 303, r.text
    did = r.headers["location"].rsplit("/", 1)[1]
    return web.DRAFTS[did]


def visible(markup: str) -> str:
    """The text a reader sees: scripts, styles and tags removed."""
    markup = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


# --- P1.1: extraction errors in one Indonesian sentence -----------------------
def test_extraction_error_is_one_indonesian_sentence(client, monkeypatch):
    d = drop(client, monkeypatch, {"bad.png": bad()})
    markup = client.get(f"/employee/drafts/{d['id']}").text
    text = visible(markup)
    assert "Berkas tidak bisa dibaca. Isi kartu ini secara manual." in text
    for raw in ("Could not read", "TesseractNotFoundError", "fill in the card by hand"):
        assert raw not in text
    assert f'title="{BAD_ERROR}"' in markup
    assert d["cards"][0]["files"][0]["error"] == BAD_ERROR


# --- P1.2: summary banner ------------------------------------------------------
def test_summary_banner_lists_unreadable_files_and_incomplete_cards(client, monkeypatch):
    d = drop(client, monkeypatch, {"bad.png": bad(),
                                   "ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    markup = client.get(f"/employee/drafts/{d['id']}").text
    assert 'class="alert"' in markup and 'role="status"' in markup
    assert "1 dari 2 berkas belum terbaca." in markup
    bad_card = d["cards"][0]["id"]
    assert "Lengkapi kartu <a href='#card-" in markup
    assert f"href='#card-{bad_card}'" in markup
    assert f"id='card-{bad_card}'" in markup
    assert "Lengkapi kartu 1" in visible(markup)


def test_summary_banner_is_absent_when_everything_is_readable(client, monkeypatch):
    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    markup = client.get(f"/employee/drafts/{d['id']}").text
    assert 'class="alert"' not in markup
    assert "berkas belum terbaca" not in visible(markup)


# --- P1.3/P1.4: stepper and no internal terms ---------------------------------
def test_both_pages_show_the_stepper_and_no_internal_terms(client, monkeypatch):
    new = client.get("/employee/new").text
    assert 'class="stepper"' in new
    assert "Perjalanan" in new and "Unggah berkas" in new and "Periksa &amp; kirim" in new
    assert "Ekstraksi:" not in new
    assert "1. Perjalanan" not in new and "2. Unggah" not in new

    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    draft = client.get(f"/employee/drafts/{d['id']}").text
    assert 'class="stepper"' in draft
    assert '<li aria-current="step">Periksa &amp; kirim</li>' in draft
    assert "3. Periksa" not in draft
