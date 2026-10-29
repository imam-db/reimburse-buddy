"""ADR 0009: one claim state everywhere, evidence finance can see, and the page fixes that follow."""
import re

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import plans, qa

HERO = "CLM-2026-0110"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-004")
    web.reset_all()
    qa.STATE.clear()
    yield TestClient(web.app)
    qa.STATE.clear()
    web.reset_all()


def card_gaps(html: str, cid: str) -> set[str]:
    card = html.split(f'id="exception-{cid}"', 1)[1].split("</article>", 1)[0]
    return set(re.findall(r'<span class="tag " title="([a-z_]+)"', card))


def test_triage_qa_and_claim_page_agree_after_review(client):
    for ev in ("MSG-5201", "MSG-5001", "MEMO-2026-038", "EVT-7010", "MSG-5202", "MSG-5203"):
        client.post(f"/admin/claims/{HERO}/evidence", data={"evidence_id": ev})
    client.post(f"/admin/claims/{HERO}/decide", data={"item_id": "IT-3", "choice": "approve_capped"})
    reviewed = {g for i in web.run_check(HERO).items for g in i.gaps}
    assert reviewed != {g for i in web.check_claim(web.store, HERO).items for g in i.gaps}  # review changed something
    triage = card_gaps(client.get("/admin").text, HERO)
    qa_gaps = {g for i in qa._Data().check(HERO)["items"] for g in i["gaps"]}
    page = client.get(f"/admin/claims/{HERO}").text
    page_gaps = set(re.findall(r"<span class='tag warn' title='([a-z_]+)'>", page))
    assert triage == qa_gaps == page_gaps == reviewed


def test_time_saved_assumption_is_indonesian(client):
    r = client.get("/admin")
    assert "15 menit tinjauan manual" in r.text
    assert "min of manual review" not in r.text and "Assumed average" not in r.text


def _submit_web_claim():
    web.store.claims.append({"id": "CLM-2026-W001", "employee_id": "EMP-001", "status": "submitted",
                             "submitted_at": "2026-10-29", "purpose": "Demo", "batch": web.WEB_BATCH,
                             "trip": {"origin": "Jakarta", "destination": "Surabaya",
                                      "start_date": "2026-10-12", "end_date": "2026-10-13"},
                             "items": [{"id": "IT-1", "category": "taxi", "date": "2026-10-12",
                                        "description": "Taksi", "amount": "100000", "receipt": True}]})


def test_new_employee_claims_notice_links_to_web_batch(client):
    assert "klaim baru dari karyawan" not in client.get("/admin").text
    _submit_web_claim()
    r = client.get("/admin")
    assert "1 klaim baru dari karyawan menunggu triage" in r.text
    assert "/admin?batch=web-submissions" in r.text
    assert "10 klaim, 7 bersih, 3 pengecualian" in r.text  # the default numbers do not change
    assert "klaim baru dari karyawan" not in client.get("/admin?batch=web-submissions").text


def test_batch_dropdown_has_readable_names_and_same_values(client):
    r = client.get("/admin")
    assert "2026-W44 (persetujuan sendiri)" in r.text and "Pengajuan karyawan" in r.text
    assert 'value="web-submissions"' in r.text and 'value="2026-W44"' in r.text


# --- item 3: finance opens uploaded evidence ---------------------------------------------------
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")


def add_file_message(tmp_path, mid="MSG-W901", cid=HERO, name="MSG-W901_struk.png", **extra):
    (tmp_path / name).write_bytes(PNG)
    att = {"name": "struk.png", "type": "receipt", "claim_id": cid, "item_id": "IT-5", "stored_as": name,
           "linked_claim": cid, "linked_item": "IT-5", **extra}
    web.store.inbox.append({"id": mid, "owner_id": "EMP-001", "from": "x@y.example", "date": "2026-10-29",
                            "subject": "s", "body": "", "attachments": [att], "mailbox": web.MAILBOX})


def test_finance_file_route_serves_with_safe_headers(client, tmp_path):
    add_file_message(tmp_path)
    r = client.get(f"/admin/claims/{HERO}/files/MSG-W901")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["cache-control"] == "private, no-store"


def test_finance_file_route_404s(client, tmp_path):
    add_file_message(tmp_path)
    assert client.get(f"/admin/claims/{HERO}/files/MSG-NOPE").status_code == 404
    assert client.get("/admin/claims/CLM-2026-0101/files/MSG-W901").status_code == 404  # message of another claim
    assert client.get("/admin/claims/CLM-NOPE/files/MSG-W901").status_code == 404
    assert client.get(f"/admin/claims/{HERO}/files/MSG-5201").status_code == 404  # seed message, no stored file


def test_finance_file_route_stays_inside_upload_dir_and_allowlist(client, tmp_path):
    outside = tmp_path.parent / "secret.png"
    outside.write_bytes(PNG)
    add_file_message(tmp_path, mid="MSG-W902", name="../secret.png")
    assert client.get(f"/admin/claims/{HERO}/files/MSG-W902").status_code == 404
    add_file_message(tmp_path, mid="MSG-W903", name="MSG-W903_notes.txt")
    assert client.get(f"/admin/claims/{HERO}/files/MSG-W903").status_code == 404


def test_claim_page_links_evidence_and_shows_thumbnail(client, tmp_path):
    add_file_message(tmp_path)
    web.review(HERO)["pending"]["MSG-W901"] = {"item_id": "IT-5", "reason": "self_declared_only",
                                              "reasons": ["self_declared_only"], "state": "pending"}
    page = client.get(f"/admin/claims/{HERO}").text
    assert f'href="/admin/claims/{HERO}/files/MSG-W901"' in page and "evidence-thumb" in page
    web.review(HERO)["pending"]["MSG-W901"]["state"] = "accepted"
    web.review(HERO)["evidence"].append("MSG-W901")
    page = client.get(f"/admin/claims/{HERO}").text
    assert 'class="tag ok" href="/admin/claims/' in page


def test_employee_and_manager_cannot_open_finance_file_route(tmp_path, monkeypatch):
    from conftest import install_test_users, login_as
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    web.reset_all()
    install_test_users()
    add_file_message(tmp_path)
    c = TestClient(web.app)
    url = f"/admin/claims/{HERO}/files/MSG-W901"
    assert c.get(url, follow_redirects=False).status_code == 303  # not signed in: the login form
    for emp in ("EMP-001", "EMP-100"):
        login_as(c, emp, "/")
        denied = c.get(url, follow_redirects=False)
        assert denied.status_code == 303 and denied.headers["location"] != url and denied.content != PNG
        assert c.post(url).status_code == 403
    login_as(c, "EMP-102")
    assert c.get(url).status_code == 200
    web.reset_all()


# --- item 4: Indonesian findings ---------------------------------------------------------------
def visible_text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)  # tags (and so their title attributes) are dropped


def test_claim_page_findings_are_indonesian(client):
    for ev in ("MSG-5201", "MSG-5001", "MEMO-2026-038", "EVT-7010", "MSG-5202", "MSG-5203"):
        client.post(f"/admin/claims/{HERO}/evidence", data={"evidence_id": ev})
    client.post(f"/admin/claims/{HERO}/decide", data={"item_id": "IT-3", "choice": "approve_capped"})
    page = client.get(f"/admin/claims/{HERO}").text
    text = visible_text(page.split('id="decision-note-box"', 1)[0])  # the note has its own English toggle
    for english in ("Receipt found in", "exceeds the tier", "Resolved by the reviewer", "approve_capped",
                    "Boarding pass for", "Category: "):
        assert english not in text, english
    assert "ditemukan di MSG-5201" in text and "Bukti pembayaran tidak ada" in text
    assert "Diputuskan oleh reviewer: Setujui sebatas plafon" in text
    assert "Boarding pass for QZ 7520" in page  # the English text stays in the title attribute


def test_provenance_tags_use_indonesian_names():
    html = web.provenance_badges({"provenance": {"category": "ocr", "date": "ocr", "amount": "self_declared",
                                                 "merchant": "pdf_text", "nights": "ocr", "flight_no": "ocr"}})
    text = visible_text(html)
    for word in ("Kategori: OCR", "Tanggal: OCR", "Nominal: Dideklarasikan sendiri", "Merchant: Teks PDF",
                 "Malam: OCR", "No. penerbangan: OCR"):
        assert word in text, word
