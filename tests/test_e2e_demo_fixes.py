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
    assert qa_gaps == page_gaps == reviewed
    # Triage lists only open work: the hotel cap was decided, so its chip is gone from the card.
    assert "hotel_over_cap" in reviewed and "hotel_over_cap" not in triage
    assert triage == {g for i in web.run_check(HERO).items for g in web.open_gaps(i)}


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


# --- item 5: item status ------------------------------------------------------------------------
def item_status(page: str, iid: str) -> str:
    card = page.split(f"id='item-{iid}'", 1)[1].split("</article>", 1)[0]
    return visible_text(card)


def test_item_with_only_a_decision_gap_says_perlu_keputusan(client):
    page = client.get(f"/admin/claims/{HERO}").text
    hotel, taxi = item_status(page, "IT-3"), item_status(page, "IT-5")
    assert "Perlu keputusan" in hotel and "Perlu bukti" not in hotel
    assert "Perlu bukti" in taxi and "Perlu keputusan" not in taxi  # a document gap is still a document gap
    assert "Disetujui: Rp2.400.000" in hotel  # amounts do not change


# --- item 6: compact sticky summary -------------------------------------------------------------
def test_sticky_summary_is_one_line_and_the_rest_scrolls(client):
    page = client.get(f"/admin/claims/{HERO}").text
    section = page.split('<section class="claim-summary"', 1)[1].split("</section>", 1)[0]
    line, rest = section.split('<div class="summary-rest">', 1)
    line_text = visible_text(line)
    assert HERO in line_text and "Disetujui" in line_text and "tertunda" in line_text
    assert "Selidiki dengan agent" in line_text
    assert "Mesin aturan" not in line_text and web.store.get_claim(HERO)["purpose"] not in line_text
    assert web.store.get_claim(HERO)["purpose"] in rest
    assert "Bukti terlampir" in rest and "id='needs-you'" in rest
    css = (web.STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".summary-line" in css and "--header-h" in css and "scroll-margin-top" in css


# --- item 7: ask for the document where the gap is ----------------------------------------------
def test_item_with_a_document_gap_has_a_prefilled_request_form(client):
    page = client.get(f"/admin/claims/{HERO}").text
    taxi = page.split("id='item-IT-5'", 1)[1].split("</article>", 1)[0]
    assert "Minta dokumen ke karyawan" in taxi and f"action='/admin/claims/{HERO}/message'" in taxi
    assert "Klaim CLM-2026-0110: dokumen kurang" in taxi
    assert "Mohon kirim struk untuk Taksi (IT-5) tanggal 3 Okt 2026." in taxi
    hotel = page.split("id='item-IT-3'", 1)[1].split("</article>", 1)[0]
    assert "Minta dokumen" not in hotel  # only a decision is open there
    assert "struk taksi tanggal 3 Okt (IT-5)" not in page.split('id="manual"', 1)[1]  # no hero placeholder


def test_sending_the_request_flashes_the_employee_name(client):
    r = client.post(f"/admin/claims/{HERO}/message", data={"subject": "s", "body": "Mohon kirim struk."})
    assert "Pesan terkirim ke Rina Wulandari." in r.text
    assert "OUT-" not in r.text.split("<main", 1)[1].split("Pesan terkirim", 1)[0]
    assert any(m["to"] == "EMP-001" and m.get("claim_id") == HERO for m in web.store.outbox)


# --- item 17: landing page ------------------------------------------------------------------------
def test_landing_cards_link_somewhere_and_the_guide_has_the_employee_path(client):
    html = client.get("/").text
    assert '<h2><a href="/admin">Finance</a></h2>' in html and '<h2><a href="/employee">Karyawan</a></h2>' in html
    assert '<a class="btn" href="/employee/new">Kirim klaim sebagai karyawan</a>' in html
    assert '<a class="btn" href="/admin">Lihat triage finance</a>' in html  # the finance links stay


def test_reset_lives_in_a_closed_demo_tools_disclosure(client):
    html = client.get("/").text
    details = html.split('<details id="demo-tools">', 1)[1].split("</details>", 1)[0]
    assert "<summary>Alat demo</summary>" in details and 'action="/admin/reset"' in details
    assert "Reset demo" in details
    assert "<details id=\"demo-tools\" open" not in html
    assert 'action="/admin/reset"' not in html.split('<details id="demo-tools">', 1)[0]


def test_only_the_indonesian_footer_remains(client):
    html = client.get("/").text.split("<main", 1)[1]  # the meta description is not a footer
    assert "Synthetic data" not in html and html.count("Data sintetis.") == 1
    assert 'id="footer-note"' in html


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


def test_document_request_for_a_flight_asks_for_the_ticket(client):
    web.store.get_claim(HERO)["items"][0]["receipt"] = False  # IT-1 flight: no ticket and no boarding pass
    page = client.get(f"/admin/claims/{HERO}").text
    flight = page.split("id='item-IT-1'", 1)[1].split("</article>", 1)[0]
    assert "Mohon kirim tiket pesawat (e-ticket) dan boarding pass untuk Penerbangan (IT-1)" in flight
    assert "Mohon kirim struk" not in flight


def test_item_card_does_not_repeat_the_category_code_as_its_subtitle(client):
    """A flight with no merchant has description "flight"; the card shows "Penerbangan" once, not "flight"."""
    web.store.claims.append({"id": "CLM-2026-W001", "employee_id": "EMP-001", "status": "submitted",
                             "submitted_at": "2026-10-29", "purpose": "Demo", "batch": web.WEB_BATCH,
                             "trip": {"origin": "Jakarta", "destination": "Surabaya",
                                      "start_date": "2026-10-02", "end_date": "2026-10-04"},
                             "items": [{"id": "IT-1", "category": "flight", "date": "2026-10-02",
                                        "description": "flight", "amount": "1120000", "flight_no": "QZ 7520",
                                        "receipt": False, "boarding_pass": False},
                                       {"id": "IT-2", "category": "taxi", "date": "2026-10-03",
                                        "description": "TAKSI KOTA BIRU", "amount": "230000", "receipt": False}]})
    html = client.get("/admin/claims/CLM-2026-W001").text
    flight = html.split("id='item-IT-1'")[1].split("</article>")[0]
    taxi = html.split("id='item-IT-2'")[1].split("</article>")[0]
    assert "<div class='note'>flight</div>" not in flight
    assert "<div class='note'>TAKSI KOTA BIRU</div>" in taxi
