import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web

HERO = "CLM-2026-0110"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def test_triage_page_is_read_only(client):
    r = client.get("/admin")
    assert r.status_code == 200
    assert "10 claims, 7 cleared, 3 exceptions" in r.text and "estimate" in r.text
    assert web.store.status_log == []


def test_bulk_approve_needs_confirmation(client):
    cleared = web.triage_claims(web.store, web.BATCH)["auto_cleared"]
    r = client.post("/admin/batch/approve", data={"batch": web.BATCH, "claim_ids": cleared})
    assert "Not approved" in r.text
    assert web.store.status_log == []
    client.post("/admin/batch/approve", data={"batch": web.BATCH, "claim_ids": cleared, "reviewer_confirmed": "true"})
    assert all(web.store.get_claim(c)["status"] == "approved" for c in cleared)


def test_hero_reviewer_flow(client):
    for ev in ("MSG-5201", "MSG-5001", "MEMO-2026-038", "EVT-7010", "MSG-5202"):
        client.post(f"/admin/claims/{HERO}/evidence", data={"evidence_id": ev})
    client.post(f"/admin/claims/{HERO}/message", data={"subject": "Claim CLM-2026-0110: taxi receipt",
                                                      "body": "Mohon kirim struk taksi tanggal 3 Okt (IT-5)."})
    client.post(f"/admin/claims/{HERO}/evidence", data={"evidence_id": "MSG-5203"})
    client.post(f"/admin/claims/{HERO}/decide", data={"item_id": "IT-3", "choice": "approve_capped"})
    r = client.get(f"/admin/claims/{HERO}")
    assert "Rp6.590.000</b> of Rp6.890.000" in r.text
    assert "memo_unsigned" in r.text and "no_matching_item" in r.text
    assert web.store.get_claim(HERO)["status"] == "submitted"
    client.post(f"/admin/claims/{HERO}/status", data={"status": "partially_approved"})
    assert web.store.get_claim(HERO)["status"] == "submitted"  # no confirmation tick
    client.post(f"/admin/claims/{HERO}/status", data={"status": "partially_approved", "reviewer_confirmed": "true"})
    assert web.store.get_claim(HERO)["status"] == "partially_approved"


def test_employee_submit_and_forward(client):
    r = client.post("/employee/claims", data={"emp": "EMP-004", "purpose": "Vendor visit", "destination": "Bandung",
                                              "start_date": "2026-10-20", "end_date": "2026-10-21",
                                              "items": "taxi, 2026-10-20, Station taxi, 150000, no"})
    assert "Submitted+CLM-2026-W001" in str(r.url) and "CLM-2026-W001" in r.text
    assert web.triage_claims(web.store, web.BATCH)["claims"] == 10  # web claims use their own batch
    r = client.post("/employee/forward", data={"emp": "EMP-004", "subject": "Taxi receipt", "claim_id": "CLM-2026-W001",
                                               "att_type": "receipt", "item_id": "IT-1"},
                    files={"file": ("taxi.txt", b"synthetic receipt", "text/plain")})
    assert "MSG-W001" in r.text
    assert web.run_check("CLM-2026-W001").items[0].gaps == ["receipt_missing"]
    web.review("CLM-2026-W001")["evidence"].append("MSG-W001")
    assert web.run_check("CLM-2026-W001").items[0].gaps == []


def test_bad_item_line_rejected(client):
    r = client.post("/employee/claims", data={"emp": "EMP-004", "purpose": "x", "destination": "Bandung",
                                              "start_date": "2026-10-20", "end_date": "2026-10-21", "items": "nonsense"})
    assert r.status_code == 400


def test_employee_sees_finance_messages(client):
    client.post(f"/admin/claims/{HERO}/message", data={"subject": "Claim CLM-2026-0110: taxi receipt", "body": "Mohon kirim struk (IT-5)."})
    assert "Mohon kirim struk" in client.get("/employee?emp=EMP-001").text
    assert client.get("/admin/claims/CLM-NOPE").status_code == 404
