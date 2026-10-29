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


def form_of(d: dict) -> dict:
    out = {}
    for c in d["cards"]:
        for k, v in c["fields"].items():
            out[f"c_{c['id']}_{k}"] = "" if v["value"] is None else str(v["value"])
        for f in c["files"]:
            out[f"f_{f['id']}_doc_type"] = f["doc_type"]
    return out


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


# --- P1.5: sticky submit bar ---------------------------------------------------
def test_submit_bar_shows_items_total_and_incomplete(client, monkeypatch):
    from decimal import Decimal

    from reimburse_buddy.models import rupiah

    d = drop(client, monkeypatch, {
        "a.png": good(doc_type="taxi_receipt", date="2026-10-03", amount=150000, merchant="Taksi"),
        "b.png": good(doc_type="meal_receipt", date="2026-10-03", amount=95000, merchant="Warung"),
        "bad.png": bad(),
    })
    markup = client.get(f"/employee/drafts/{d['id']}").text
    assert 'id="draft-bar"' in markup
    assert "3 item" in markup
    assert f"Total diajukan {rupiah(Decimal('245000'))}" in markup
    assert "1 kartu belum lengkap" in markup
    assert 'class="btn primary"' in markup and 'value="submit"' in markup
    assert 'value="save"' in markup
    assert 'style="font-weight:bold"' not in markup
    assert markup.index('id="draft-cards"') < markup.index('value="add"') < markup.index('id="draft-bar"')
    assert 'class="draft-spacer"' in markup


# --- P1.6: busy state on long submits ------------------------------------------
def test_busy_scripts_and_no_js_submit(client, monkeypatch):
    new = client.get("/employee/new").text
    assert 'id="drop-form"' in new and 'id="read-files"' in new
    assert "aria-busy" in new and "Membaca" in new

    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    draft = client.get(f"/employee/drafts/{d['id']}").text
    assert 'id="draft-form"' in draft and 'id="submit-claim"' in draft
    assert "aria-busy" in draft and "Mengirim" in draft

    r = client.post(f"/employee/drafts/{d['id']}", data=dict(form_of(d), action="submit"),
                    follow_redirects=False)
    assert r.status_code == 303 and "/employee/claims/" in r.headers["location"]


# --- P2.7: secondary actions behind one disclosure -----------------------------
def test_secondary_actions_live_behind_one_disclosure(client, monkeypatch):
    d = drop(client, monkeypatch, {
        "a.png": good(doc_type="taxi_receipt", date="2026-10-03", amount=150000, merchant="Taksi"),
        "b.png": good(doc_type="meal_receipt", date="2026-10-03", amount=95000, merchant="Warung"),
    })
    a, b = (c["id"] for c in d["cards"])
    fid = d["cards"][0]["files"][0]["id"]
    markup = client.get(f"/employee/drafts/{d['id']}").text
    assert "<summary>Atur kartu</summary>" in markup
    assert f"name='move_{fid}'" in markup and f"value='move:{fid}'" in markup
    assert f"name='merge_{a}'" in markup and f"value='merge:{a}'" in markup
    assert f"value='delete:{a}'" in markup and "Hapus item ini?" in markup
    assert markup.index("<summary>Atur kartu</summary>") < markup.index(f"value='delete:{a}'")

    r = client.post(f"/employee/drafts/{d['id']}",
                    data=dict(form_of(d), action=f"move:{fid}", **{f"move_{fid}": b}),
                    follow_redirects=False)
    assert r.status_code == 303
    assert len(d["cards"][0]["files"]) == 0 and len(d["cards"][1]["files"]) == 2

    r = client.post(f"/employee/drafts/{d['id']}",
                    data=dict(form_of(d), action=f"merge:{a}", **{f"merge_{a}": b}),
                    follow_redirects=False)
    assert r.status_code == 303 and len(d["cards"]) == 1
    merged = d["cards"][0]["id"]
    markup = client.get(f"/employee/drafts/{d['id']}").text
    assert f"value='split:{merged}'" in markup

    r = client.post(f"/employee/drafts/{d['id']}",
                    data=dict(form_of(d), action=f"split:{merged}"), follow_redirects=False)
    assert r.status_code == 303 and len(d["cards"]) == 2

    first = d["cards"][0]["id"]
    r = client.post(f"/employee/drafts/{d['id']}",
                    data=dict(form_of(d), action=f"delete:{first}"), follow_redirects=False)
    assert r.status_code == 303 and len(d["cards"]) == 1


# --- P2.8: card anchors and return position ------------------------------------
def test_cards_have_anchors_and_a_focus_script(client, monkeypatch):
    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    markup = client.get(f"/employee/drafts/{d['id']}").text
    cid = d["cards"][0]["id"]
    assert f"id='card-{cid}'" in markup and "tabindex='-1'" in markup
    assert "location.hash" in markup and "prefers-reduced-motion" in markup and "scrollIntoView" in markup


def test_save_returns_to_the_first_card_with_an_error_or_nowhere(client, monkeypatch):
    d = drop(client, monkeypatch, {"bad.png": bad(),
                                   "ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    bad_card = d["cards"][0]["id"]
    r = client.post(f"/employee/drafts/{d['id']}", data=dict(form_of(d), action="save"),
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/employee/drafts/{d['id']}#card-{bad_card}"

    form = dict(form_of(d), **{f"c_{bad_card}_category": "taxi", f"c_{bad_card}_date": "2026-10-03",
                               f"c_{bad_card}_amount": "50000", f"c_{bad_card}_merchant": "Taksi"})
    r = client.post(f"/employee/drafts/{d['id']}", data=dict(form, action="save"),
                    follow_redirects=False)
    assert r.headers["location"] == f"/employee/drafts/{d['id']}"


def test_add_returns_to_the_new_card(client, monkeypatch):
    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    r = client.post(f"/employee/drafts/{d['id']}", data={"action": "add"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/employee/drafts/{d['id']}#card-{d['cards'][-1]['id']}"


def test_move_merge_split_and_delete_return_to_their_anchor(client, monkeypatch):
    d = drop(client, monkeypatch, {
        "a.png": good(doc_type="taxi_receipt", date="2026-10-03", amount=150000, merchant="Taksi"),
        "b.png": good(doc_type="meal_receipt", date="2026-10-03", amount=95000, merchant="Warung"),
    })
    a, b = (c["id"] for c in d["cards"])
    fid = d["cards"][0]["files"][0]["id"]

    r = client.post(f"/employee/drafts/{d['id']}",
                    data=dict(form_of(d), action=f"move:{fid}", **{f"move_{fid}": b}),
                    follow_redirects=False)
    assert r.headers["location"] == f"/employee/drafts/{d['id']}#card-{a}"

    r = client.post(f"/employee/drafts/{d['id']}",
                    data=dict(form_of(d), action=f"merge:{a}", **{f"merge_{a}": b}),
                    follow_redirects=False)
    assert r.headers["location"] == f"/employee/drafts/{d['id']}#card-{b}"

    merged = d["cards"][0]["id"]
    r = client.post(f"/employee/drafts/{d['id']}",
                    data=dict(form_of(d), action=f"split:{merged}"), follow_redirects=False)
    first = d["cards"][0]["id"]
    assert r.headers["location"] == f"/employee/drafts/{d['id']}#card-{first}"

    r = client.post(f"/employee/drafts/{d['id']}",
                    data=dict(form_of(d), action=f"delete:{first}"), follow_redirects=False)
    assert r.headers["location"] == f"/employee/drafts/{d['id']}#draft-cards"
