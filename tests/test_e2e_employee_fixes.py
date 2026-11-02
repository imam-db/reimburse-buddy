"""ADR 0009, employee side (P2). Stub extractors only: nothing here needs Tesseract."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import upload_flow

from test_upload_flow_ux import bad, drop, form_of, good, visible


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-001")
    monkeypatch.delenv("EMPLOYEE_QA", raising=False)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def web_claim(items, cid="CLM-2026-W050", emp="EMP-001"):
    web.store.claims.append({"id": cid, "employee_id": emp, "status": "submitted", "submitted_at": "2026-10-29",
                             "purpose": "Demo", "batch": web.WEB_BATCH,
                             "trip": {"origin": "Jakarta", "destination": "Surabaya",
                                      "start_date": "2026-10-02", "end_date": "2026-10-04"}, "items": items})
    return cid


def row_text(page: str, iid: str) -> str:
    return visible(page.split(f"id='item-{iid}'", 1)[1].split("</tr>", 1)[0])


def test_flight_gap_reads_ticket_for_flights_and_receipt_otherwise(client):
    cid = web_claim([
        {"id": "IT-1", "category": "flight", "date": "2026-10-02", "description": "QZ 7520", "amount": "900000",
         "receipt": False, "flight_no": "QZ 7520", "boarding_pass": False},
        {"id": "IT-2", "category": "taxi", "date": "2026-10-03", "description": "Taksi", "amount": "100000",
         "receipt": False}])
    page = client.get(f"/employee/claims/{cid}").text
    assert "Tiket pesawat belum ada" in row_text(page, "IT-1")
    assert "Struk belum ada" not in row_text(page, "IT-1")
    assert "Struk belum ada" in row_text(page, "IT-2") and "Tiket pesawat" not in row_text(page, "IT-2")
    # finance keeps the engine label
    assert "Struk belum ada" in visible(client.get(f"/admin/claims/{cid}").text)


def add_upload(cid, iid, state, mid="MSG-W910", emp="EMP-001", name="struk.png"):
    web.store.inbox.append({"id": mid, "owner_id": emp, "from": "x@y.example", "date": "2026-10-29", "subject": "s",
                            "body": "", "mailbox": web.MAILBOX,
                            "attachments": [{"name": name, "type": "receipt", "claim_id": cid, "item_id": iid,
                                             "linked_claim": cid, "linked_item": iid, "stored_as": "x.png"}]})
    rv = web.review(cid)
    rv.setdefault("origin", {})[mid] = "employee_upload"
    rv["pending"][mid] = {"item_id": iid, "reason": "duplicate_file", "reasons": ["duplicate_file"], "state": state}


def test_rejected_upload_tells_the_employee_without_the_reason(client):
    cid = web_claim([{"id": "IT-1", "category": "taxi", "date": "2026-10-03", "description": "Taksi",
                      "amount": "100000", "receipt": False}])
    add_upload(cid, "IT-1", "rejected")
    page = client.get(f"/employee/claims/{cid}").text
    text = row_text(page, "IT-1")
    assert "! Perlu tindakan: Berkas sebelumnya tidak diterima finance. Unggah ulang." in text
    assert "struk.png (tidak diterima)" in text
    for leak in ("duplicate_file", "duplikat", "Duplikat", "Struk belum ada"):
        assert leak not in text
    chip = re.search(r'<span class="([^"]*)"[^>]*>struk\.png \(tidak diterima\)</span>', page)
    assert chip and chip.group(1).strip() == "tag"  # neutral tone, not ok or warn


def test_rejected_upload_stays_masked_on_a_sensitive_item(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-003")
    cid = "CLM-2026-0104"
    chk = web.run_check(cid)
    iid = next(i.item_id for i in chk.items if "duplicate_suspected" in i.gaps)
    add_upload(cid, iid, "rejected", emp="EMP-003")
    text = row_text(client.get(f"/employee/claims/{cid}").text, iid)
    assert "tidak diterima" not in text and "Unggah ulang" not in text
    assert "Sedang ditinjau" in text or "ditinjau" in text


def test_portal_message_links_to_the_claim_page(client):
    web.store.send_message("EMP-001", "Mohon kirim struk.", "Klaim CLM-2026-0110", "CLM-2026-0110")
    page = client.get("/employee").text
    box = page.split("Pesan dari finance", 1)[1]
    assert "<a href='/employee/claims/CLM-2026-0110'>CLM-2026-0110</a>" in box


def test_anchor_targets_clear_the_sticky_header():
    css = (web.STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "--header-h:" in css
    block = css.split('[id^="card-"],', 1)[1].split("}", 1)[0]
    assert '[id^="item-"]' in block and "#draft-cards" in block
    assert "scroll-margin-top: calc(var(--header-h)" in block


def test_submit_bar_renders_the_server_total_first_and_ships_the_live_script(client, monkeypatch):
    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    page = client.get(f"/employee/drafts/{d['id']}").text
    assert '<span id="bar-total">Total diajukan Rp150.000</span>' in page
    assert '<span id="bar-incomplete">0 kartu belum lengkap</span>' in page
    assert 'data-start="2026-10-02"' in page and 'data-end="2026-10-04"' in page
    assert "getElementById('bar-total')" in page  # the script is there; the page still works without it


def test_reasons_show_as_hints_before_the_first_save_and_as_errors_after(client, monkeypatch):
    d = drop(client, monkeypatch, {"bad.png": bad()})
    page = client.get(f"/employee/drafts/{d['id']}").text
    assert "class='hint note'>Pilih kategori." in page and "class='hint note'>Isi nominal." in page
    assert "<div class=err>Isi nominal.</div>" not in page
    client.post(f"/employee/drafts/{d['id']}", data={**form_of(d), "action": "save"})
    page = client.get(f"/employee/drafts/{d['id']}").text
    assert "<div class=err>Isi nominal.</div>" in page and "class='hint note'" not in page


def test_banner_lists_the_card_and_its_first_reason(client, monkeypatch):
    d = drop(client, monkeypatch, {"bad.png": bad(),
                                   "ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    page = client.get(f"/employee/drafts/{d['id']}").text
    banner = page.split("Lengkapi:", 1)[1].split("</div>", 1)[0]
    assert "Kartu 1</a>: kategori belum dipilih" in banner and "Kartu 2" not in banner


def test_empty_amount_and_malformed_amount_have_different_messages(client, monkeypatch):
    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    card = d["cards"][0]
    h = d["header"]
    card["fields"]["amount"]["value"] = None
    assert "Isi nominal." in upload_flow._card_errors(card, h)
    card["fields"]["amount"]["value"] = "abc"
    errors = upload_flow._card_errors(card, h)
    assert "Nominal harus bilangan bulat rupiah, misalnya 1.350.000." in errors and "Isi nominal." not in errors


def test_boarding_pass_only_flight_card_explains_the_missing_price(client, monkeypatch):
    d = drop(client, monkeypatch, {"bp.png": good(doc_type="boarding_pass", date="2026-10-02",
                                                  flight_no="QZ 7520")})
    page = client.get(f"/employee/drafts/{d['id']}").text
    assert "Boarding pass tidak memuat harga. Tambahkan e-ticket atau isi harga tiket." in page
    assert "Isi nominal." in page
