"""ADR 0008: honest item states, one next step and a shorter portal for the employee pages.

No test here needs Tesseract: claims and evidence are built in the store, or go through a stub extractor.
"""

from __future__ import annotations

import html
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from reimburse_buddy.models import ItemResult, ItemStatus
from reimburse_web import app as web
from reimburse_web import upload_flow
from reimburse_web.extract import Draft, Field

HERO = "CLM-2026-0110"
FIELDS = ("doc_type", "date", "amount", "merchant", "flight_no", "nights", "passenger")
TRIP = dict(purpose="Client visit", destination="Surabaya", start_date="2026-10-02", end_date="2026-10-04")


class StubExtractor:
    name = "stub"

    def extract(self, path):
        d = Draft(fields={k: Field() for k in FIELDS})
        d.error = "Could not read this file (stub); fill in the card by hand."
        return d


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-001")
    monkeypatch.delenv("EMPLOYEE_QA", raising=False)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def visible(markup: str) -> str:
    markup = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def submit_new_claim(client, monkeypatch) -> str:
    """A claim through the real new-claim flow: two unreadable files, so both wait for a reviewer."""
    monkeypatch.setattr(upload_flow, "get_extractor", lambda: StubExtractor())
    files = [("files", (n, b"\x89PNG\r\n\x1a\n")) for n in ("taxi.png", "meal.png")]
    r = client.post("/employee/drafts", data=TRIP, files=files, follow_redirects=False)
    d = web.DRAFTS[r.headers["location"].rsplit("/", 1)[1]]
    data = {}
    for c, (cat, amount) in zip(d["cards"], (("taxi", "150000"), ("meal", "80000"))):
        data.update({f"c_{c['id']}_category": cat, f"c_{c['id']}_date": "2026-10-03",
                     f"c_{c['id']}_amount": amount, f"c_{c['id']}_merchant": "",
                     **{f"f_{f['id']}_doc_type": f["doc_type"] for f in c["files"]}})
    r = client.post(f"/employee/drafts/{d['id']}", data=dict(data, action="submit"), follow_redirects=False)
    assert r.status_code == 303 and "/employee/claims/" in r.headers["location"], r.text
    return r.headers["location"].split("?")[0].rsplit("/", 1)[1]


def res(gaps=(), status=ItemStatus.NEEDS_EVIDENCE, iid="IT-1") -> ItemResult:
    return ItemResult(item_id=iid, category="hotel", claimed=Decimal(1), approvable=Decimal(0),
                      status=status, gaps=list(gaps))


# --- item 1: four honest states --------------------------------------------------
def test_item_state_precedence():
    st = upload_flow.item_state
    assert st(res(status=ItemStatus.REJECTED, gaps=["hotel_over_cap"]), {}).text == "✕ Ditolak"
    masked = st(res(gaps=["duplicate_suspected", "receipt_missing"]), {"IT-1": ["m"]})
    assert masked.text == "◷ " + upload_flow.REVIEWING and masked.masked and masked.key == "review"
    assert st(res(gaps=["receipt_amount_mismatch"]), {}).masked  # B-only gap
    assert st(res(gaps=["receipt_missing"]), {"IT-1": ["m"]}).text == "◷ Menunggu verifikasi finance"
    action = st(res(gaps=["boarding_pass_missing", "hotel_over_cap"]), {})
    assert action.key == "action" and action.tone == "warn"
    assert action.text == "! Perlu tindakan: Boarding pass belum ada"
    wait = st(res(gaps=["hotel_over_cap"]), {})
    assert wait.key == "wait" and wait.tone == "info"
    assert wait.text == "◷ Menunggu keputusan finance: Hotel di atas plafon"
    done = st(res(status=ItemStatus.OK), {})
    assert done.key == "done" and done.text == "✓ Bukti lengkap" and done.tone == "ok"


def test_masked_item_never_names_a_gap_or_counts_as_action():
    s = upload_flow.item_state(res(gaps=["duplicate_suspected", "receipt_missing"]), {})
    assert s.key == "review" and "Struk" not in s.text and "uplikat" not in s.text
    r = upload_flow.item_state(res(gaps=["duplicate_suspected"], status=ItemStatus.REJECTED), {})
    assert r.text == "✕ Ditolak" and r.masked  # rejected shows no gap, and the item stays masked for files


def test_new_claim_with_pending_files_asks_nothing_of_the_employee(client, monkeypatch):
    cid = submit_new_claim(client, monkeypatch)
    page = client.get(f"/employee/claims/{cid}").text
    assert "! Perlu tindakan" not in page
    assert page.count("◷ Menunggu verifikasi finance") == 2
    assert "Beres" not in page


def test_hero_claim_still_asks_for_the_boarding_pass(client):
    page = client.get(f"/employee/claims/{HERO}").text
    assert "! Perlu tindakan: Boarding pass belum ada" in page


# --- item 2: summary and next step -----------------------------------------------
def test_summary_for_a_claim_that_needs_an_upload(client):
    page = client.get(f"/employee/claims/{HERO}").text
    assert 'id="claim-summary"' in page and "Total diajukan" in page
    assert "Unggah berkas yang diminta. Finance baru bisa memutuskan setelah itu." in page
    first = next(i["id"] for i in web.store.get_claim(HERO)["items"]
                 if i["category"] == "flight")
    link = re.search(r"<a href='#item-([^']+)'>Ke item pertama</a>", page)
    assert link and link.group(1) == first


def test_summary_for_a_claim_waiting_on_finance(client, monkeypatch):
    cid = submit_new_claim(client, monkeypatch)
    text = visible(client.get(f"/employee/claims/{cid}").text)
    assert "Tidak ada yang perlu Anda lakukan. Finance sedang meninjau klaim ini." in text
    assert "Unggah berkas yang diminta" not in text
    assert "Total diajukan Rp230.000" in text


def test_summary_for_a_claim_with_complete_evidence(client):
    text = visible(client.get("/employee/claims/CLM-2026-0116").text)
    assert "Bukti lengkap. Tinggal menunggu keputusan finance." in text


def test_claim_page_never_shows_the_engine_totals(client):
    chk = web.run_check(HERO)
    assert chk.total_approvable != chk.total_claimed and chk.total_pending != chk.total_claimed
    page = client.get(f"/employee/claims/{HERO}").text
    for amount in (chk.total_approvable, chk.total_pending):
        assert web.rupiah(amount) not in page
    assert web.rupiah(chk.total_claimed) in page


# --- item 3: files that match the evidence ---------------------------------------
def test_hero_shows_the_mailbox_attachment_the_engine_used(client):
    web.review(HERO)["evidence"].append("MSG-5201")  # Rina's own check-in pass for QZ 7520
    chk = web.run_check(HERO)
    assert any("MSG-5201" in r.evidence_used for r in chk.items)
    page = client.get(f"/employee/claims/{HERO}").text
    assert re.search(r'<span class="tag ok"[^>]*>QZ7520_mobile_checkin.pdf</span>', page)


def test_uploaded_files_wait_in_warn_tone_and_masked_items_show_none_of_the_mailbox(client, monkeypatch):
    cid = submit_new_claim(client, monkeypatch)
    page = client.get(f"/employee/claims/{cid}").text
    assert page.count("(menunggu verifikasi)</span>") == 2
    assert len(re.findall(r'<span class="tag warn"', page)) == 2 and "taxi.png (menunggu verifikasi)" in page

    emp = web.store.get_claim(cid)["employee_id"]
    res_ = res(gaps=["duplicate_suspected"], iid="IT-1")
    res_.evidence_used = ["MSG-5201"]
    state = upload_flow.item_state(res_, {})
    assert state.masked
    cell = upload_flow.item_files(emp, cid, "IT-1", res_, state, {})
    assert "QZ7520_mobile_checkin.pdf" not in cell
    # the uploaded file of a masked item stays, in the neutral tone only
    waiting = next(iter(web.pending_items(cid).values()))
    cell = upload_flow.item_files(emp, cid, "IT-1", res_, state, {"IT-1": waiting})
    assert 'class="tag"' in cell and "tag warn" not in cell and "tag ok" not in cell


def test_a_message_owned_by_someone_else_never_shows_its_attachments(client):
    web.store.inbox.append({"id": "MSG-X1", "owner_id": "EMP-002", "from": "x", "date": "2026-10-01",
                            "subject": "s", "body": "", "attachments": [{"name": "someone_elses.pdf"}],
                            "mailbox": web.MAILBOX})
    web.review(HERO)["evidence"].append("MSG-X1")
    assert "someone_elses.pdf" not in client.get(f"/employee/claims/{HERO}").text
    res_ = res()
    res_.evidence_used = ["MSG-X1"]
    cell = upload_flow.item_files("EMP-001", HERO, "IT-1", res_, upload_flow.item_state(res_, {}), {})
    assert "someone_elses.pdf" not in cell


# --- item 4: descriptions ---------------------------------------------------------
def test_description_equal_to_the_category_code_is_not_repeated(client, monkeypatch):
    cid = submit_new_claim(client, monkeypatch)
    assert [i["description"] for i in web.store.get_claim(cid)["items"]] == ["taxi", "meal"]
    page = client.get(f"/employee/claims/{cid}").text
    assert "<div class=note>taxi</div>" not in page and "<div class=note>meal</div>" not in page
    assert "Taksi" in page  # the Indonesian category label is still there


def test_a_real_description_is_still_shown_and_escaped(client):
    web.store.get_claim(HERO)["items"][4]["description"] = "Taksi <b>bandara</b>"
    page = client.get(f"/employee/claims/{HERO}").text
    assert "<div class=note>Taksi &lt;b&gt;bandara&lt;/b&gt;</div>" in page


# --- item 5: upload control -------------------------------------------------------
def _rows(page: str) -> list[str]:
    return re.findall(r"<tr[^>]*>.*?</tr>", page.split("<tbody>", 1)[1], flags=re.S)


def test_action_rows_show_the_upload_control_and_the_others_keep_it_in_a_details(client):
    rows = _rows(client.get(f"/employee/claims/{HERO}").text)
    acting = [r for r in rows if "! Perlu tindakan" in r]
    assert acting and len(acting) < len(rows)
    for r in rows:
        assert r.count("type=file") == 1 and r.count("name=file") == 1
        assert "<form class='upload'" in r
        if r in acting:
            assert "<details" not in r
            assert 'class="btn primary"' in r
        else:
            assert "<summary>Tambah berkas</summary><form class='upload'" in r
            assert 'class="btn primary"' not in r
            assert 'class="btn"' in r  # the secondary Unggah button


def test_the_raw_file_input_is_visually_hidden_and_has_no_inline_style(client):
    page = client.get(f"/employee/claims/{HERO}").text
    for tag in re.findall(r"<input[^>]*type=file[^>]*>", page):
        assert "class='visually-hidden'" in tag and "name=file" in tag and "accept='.pdf,.png,.jpg,.jpeg'" in tag
    assert "Pilih berkas" in page and "Belum ada berkas dipilih" in page
    assert " style=" not in page and "<style" not in page


def test_old_upload_post_still_works(client, monkeypatch):
    cid = submit_new_claim(client, monkeypatch)
    before = len([m for m in web.store.inbox if m.get("owner_id") == "EMP-001"])
    r = client.post(f"/employee/claims/{cid}/items/IT-1/files", files={"file": ("again.png", b"\x89PNG\r\n\x1a\n")},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/employee/claims/{cid}?msg=")
    assert len([m for m in web.store.inbox if m.get("owner_id") == "EMP-001"]) == before + 1


# --- item 6: row anchors ----------------------------------------------------------
def test_every_item_row_has_an_anchor_and_the_summary_link_points_at_one(client):
    page = client.get(f"/employee/claims/{HERO}").text
    for it in web.store.get_claim(HERO)["items"]:
        assert f"<tr id='item-{it['id']}'>" in page
    target = re.search(r"<a href='#(item-[^']+)'>Ke item pertama</a>", page).group(1)
    assert f"id='{target}'" in page


def test_redirect_after_an_upload_lands_on_the_item_row(client, monkeypatch):
    cid = submit_new_claim(client, monkeypatch)
    r = client.post(f"/employee/claims/{cid}/items/IT-2/files", files={"file": ("again.png", b"\x89PNG\r\n\x1a\n")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/employee/claims/{cid}?msg=File+added&item=IT-2#item-IT-2"


# --- item 7: plain flash text -----------------------------------------------------
def test_flash_after_submit_is_plain_and_hides_the_mailbox(client, monkeypatch):
    cid = submit_new_claim(client, monkeypatch)
    page = client.get(f"/employee/claims/{cid}?msg=Submitted").text
    assert "Klaim diajukan. Berkas Anda sudah diteruskan ke finance." in page
    assert web.MAILBOX not in page and "reimburse@" not in page


def test_flash_after_an_upload_names_the_item(client, monkeypatch):
    cid = submit_new_claim(client, monkeypatch)
    r = client.post(f"/employee/claims/{cid}/items/IT-1/files", files={"file": ("again.png", b"\x89PNG\r\n\x1a\n")})
    assert r.status_code == 200
    text = visible(r.text)
    assert "Berkas ditambahkan ke Taksi." in text
    assert web.MAILBOX not in r.text


def test_flash_for_an_unknown_item_or_text_is_escaped(client):
    cid = HERO
    page = client.get(f"/employee/claims/{cid}?msg=File+added&item=IT-99").text
    assert "Berkas ditambahkan" in page and "IT-99" not in page.split("<main", 1)[1].split("</main>")[0]
    assert "&lt;b&gt;" in client.get(f"/employee/claims/{cid}?msg=%3Cb%3E").text
