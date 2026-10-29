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
