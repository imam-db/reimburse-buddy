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


# --- P2.9: category fields follow the select -----------------------------------
def test_category_fields_are_rendered_and_toggled(client, monkeypatch):
    d = drop(client, monkeypatch, {
        "taxi.png": good(doc_type="taxi_receipt", date="2026-10-03", amount=150000, merchant="Taksi"),
        "hotel.png": good(doc_type="hotel_receipt", date="2026-10-02", amount=900000,
                          merchant="Hotel", nights=2),
    })
    taxi, hotel = (c["id"] for c in d["cards"])
    markup = client.get(f"/employee/drafts/{d['id']}").text
    taxi_nights = re.search(rf"name='c_{taxi}_nights'[^>]*>", markup).group(0)
    taxi_flight = re.search(rf"name='c_{taxi}_flight_no'[^>]*>", markup).group(0)
    hotel_nights = re.search(rf"name='c_{hotel}_nights'[^>]*>", markup).group(0)
    assert "disabled" in taxi_nights and "disabled" in taxi_flight
    assert "disabled" not in hotel_nights
    assert "data-cat='hotel' hidden" in markup
    assert "data-role='category'" in markup
    assert "x.disabled=!on" in markup
    # The toggle only reaches category-only fields: category, date, amount and merchant never carry it.
    assert ".cat-field[data-cat]" in markup
    for k in ("category", "date", "amount", "merchant"):
        start = markup.index(f"name='c_{taxi}_{k}'")
        opening = markup[markup.rindex("<label", 0, start):start]
        assert "cat-field" not in opening and "data-cat" not in opening, k


def test_hotel_validation_still_requires_nights(client, monkeypatch):
    d = drop(client, monkeypatch, {"hotel.png": good(doc_type="hotel_receipt", date="2026-10-02",
                                                     amount=900000, merchant="Hotel")})
    r = client.post(f"/employee/drafts/{d['id']}", data=dict(form_of(d), action="submit"),
                    follow_redirects=False)
    assert r.status_code == 303 and d["id"] in web.DRAFTS
    assert "Jumlah malam wajib diisi untuk hotel." in d["cards"][0]["errors"]


# --- P2.10/P2.11: amount prefix, hint placeholder, calmer file rows ------------
def test_amount_field_has_a_prefix_and_a_hint_placeholder(client, monkeypatch):
    d = drop(client, monkeypatch, {"bad.png": bad(),
                                   "ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    markup = client.get(f"/employee/drafts/{d['id']}").text
    assert "class='amount-field'" in markup and "class='prefix'" in markup
    assert "Rp" in markup and "placeholder='contoh: 1.350.000'" in markup
    css = client.get("/static/app.css").text
    assert "::placeholder" in css and "var(--muted)" in css and ".file-row .err" in css
    assert markup.count("class='file-row'") == 2

    good_id = d["cards"][1]["id"]
    form = dict(form_of(d), **{f"c_{good_id}_amount": "95.000"})
    client.post(f"/employee/drafts/{d['id']}", data=dict(form, action="save"))
    assert d["cards"][1]["fields"]["amount"]["value"] == 95000


# --- P3.12/P3.13: trip dates, destination and purpose --------------------------
def test_trip_form_labels_placeholders_and_date_min(client):
    markup = client.get("/employee/new").text
    assert "Tanggal mulai" in markup and "Tanggal selesai" in markup
    assert 'value="Surabaya"' not in markup
    assert 'placeholder="mis. Surabaya"' in markup
    assert 'placeholder="mis. Kunjungan klien PT Contoh"' in markup
    assert 'id="start-date"' in markup and 'id="end-date"' in markup
    assert "e.min=s.value" in markup
    assert 'class="date-row"' in markup

    r = client.post("/employee/drafts", data={"purpose": "p", "destination": "Bandung",
                                              "start_date": "2026-10-04", "end_date": "2026-10-02"},
                    follow_redirects=False)
    assert r.status_code == 400


# --- P3.14: picked-file list ---------------------------------------------------
def test_picked_files_are_a_list_with_size_and_remove(client):
    markup = client.get("/employee/new").text
    assert '<ul id="picked" class="picked">' in markup
    assert "DataTransfer" in markup and "MAX=20" in markup
    assert "Lebih dari 5 MB" in markup and "Jenis berkas tidak didukung" in markup
    assert "submit.disabled=files.length>MAX" in markup
    assert "textContent=\"Hapus\"" in markup

    r = client.post("/employee/drafts", data=TRIP, files=[("files", ("x.txt", b"hello"))],
                    follow_redirects=False)
    assert r.status_code == 400


# --- P4.15: owner-only file route ---------------------------------------------
def first_file(d: dict) -> dict:
    return d["cards"][0]["files"][0]


def test_draft_file_route_serves_the_owner_with_safe_headers(client, monkeypatch):
    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    f = first_file(d)
    raw = (web.UPLOAD_DIR / f["stored"]).read_bytes()
    r = client.get(f"/employee/drafts/{d['id']}/files/{f['id']}")
    assert r.status_code == 200 and r.content == raw
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "inline" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "private, no-store"

    pdf = drop(client, monkeypatch, {"doc.pdf": good(doc_type="flight_receipt", date="2026-10-03",
                                                     amount=500000, merchant="Airlines",
                                                     flight_no="QZ 7520")})
    pf = first_file(pdf)
    r = client.get(f"/employee/drafts/{pdf['id']}/files/{pf['id']}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/pdf")


def test_draft_file_route_is_owner_only_and_rejects_unknown_paths(client, monkeypatch):
    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    f = first_file(d)
    url = f"/employee/drafts/{d['id']}/files/{f['id']}"
    unknown = client.get("/employee/drafts/nope")

    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-003")
    other = client.get(url)
    assert other.status_code == 404
    assert other.json()["detail"].replace(d["id"], "X") == \
        unknown.json()["detail"].replace("nope", "X")

    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-001")
    assert client.get(f"/employee/drafts/{d['id']}/files/nope").status_code == 404
    f["stored"] = "../escape.png"
    assert client.get(url).status_code == 404


def test_draft_file_route_needs_an_employee(client, monkeypatch):
    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    f = first_file(d)
    monkeypatch.delenv("REIMBURSE_DEV_EMPLOYEE")
    assert client.get(f"/employee/drafts/{d['id']}/files/{f['id']}").status_code == 401


# --- P4.16: thumbnails in the card ---------------------------------------------
def test_file_rows_show_previews_even_when_extraction_failed(client, monkeypatch):
    d = drop(client, monkeypatch, {
        "ok.png": good(doc_type="taxi_receipt", date="2026-10-03", amount=150000, merchant="Taksi"),
        "doc.pdf": good(doc_type="flight_receipt", date="2026-10-03", amount=500000,
                        merchant="Airlines", flight_no="QZ 7520"),
        "bad.png": bad(),
    })
    markup = client.get(f"/employee/drafts/{d['id']}").text
    files = {f["name"]: f for c in d["cards"] for f in c["files"]}
    for name, f in files.items():
        assert f"/employee/drafts/{d['id']}/files/{f['id']}" in markup, name
    ok = files["ok.png"]
    assert (f"<img class='thumb' loading='lazy' "
            f"src='/employee/drafts/{d['id']}/files/{ok['id']}' alt='Pratinjau ok.png'>") in markup
    assert "class='doc-icon' aria-hidden='true'>PDF</span>" in markup
    assert "alt='Pratinjau bad.png'" in markup
    assert "target='_blank' rel='noopener'" in markup
