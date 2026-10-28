import pytest
from fastapi.testclient import TestClient

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.rules import check_claim
from reimburse_buddy.triage import triage_claims
from reimburse_web import app as web

HERO = "CLM-2026-0110"


def add_claim(store, items, emp="EMP-004"):
    store.claims.append({"id": "CLM-T-1", "employee_id": emp, "status": "submitted", "purpose": "t",
                         "trip": {"origin": "Jakarta", "destination": "Bandung", "start_date": "2026-10-20",
                                  "end_date": "2026-10-21"}, "items": items})
    return "CLM-T-1"


def test_unknown_category_rejected():
    s = DataStore()
    cid = add_claim(s, [{"id": "IT-1", "category": "souvenir", "date": "2026-10-20", "description": "Gift",
                         "amount": "2500000", "receipt": True}])
    it = check_claim(s, cid).items[0]
    assert it.status.value == "rejected" and it.approvable == 0
    f = next(f for f in it.findings if f.key == "category_not_allowed")
    assert "souvenir" in f.message


def test_hero_and_triage_unchanged():
    s = DataStore()
    t = triage_claims(s, "2026-W43")
    assert (t["claims"], len(t["auto_cleared"]), len(t["exceptions"])) == (10, 7, 3)


def test_empty_flight_number_never_matches():
    s = DataStore()
    cid = add_claim(s, [{"id": "IT-1", "category": "flight", "date": "2026-10-20", "description": "flight",
                         "amount": "900000", "receipt": True, "boarding_pass": False}])
    s.inbox.append({"id": "MSG-FAKE", "owner_id": "EMP-004", "from": "x", "date": "2026-10-20", "subject": "bp",
                    "body": "", "attachments": [{"name": "bp.pdf", "type": "boarding_pass", "flight": "",
                                                  "date": "2026-10-20", "passenger": s.get_employee("EMP-004")["name"]}]})
    c = check_claim(s, cid, ["MSG-FAKE"])
    assert "boarding_pass_missing" in c.items[0].gaps


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-004")  # gate off: the acting employee comes from the environment
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def submit(client, items):
    return client.post("/employee/claims", data={"purpose": "p", "destination": "Bandung",
                                                 "start_date": "2026-10-20", "end_date": "2026-10-21", "items": items})


def test_web_flight_needs_number(client):
    assert submit(client, "flight, 2026-10-20, CGK-BDO, 900000, yes").status_code == 400
    assert submit(client, "flight, 2026-10-20, CGK-BDO, 900000, yes, QZ 7520").status_code == 200
    assert web.store.get_claim("CLM-2026-W001")["items"][0]["flight_no"] == "QZ 7520"


@pytest.mark.parametrize("amt,ok", [("180000.50", False), ("1,350,000", False), ("1.35.000", False), ("abc", False),
                                    ("1.350.000", True), ("1350000", True)])
def test_web_amount_parser(client, amt, ok):
    r = submit(client, f"taxi, 2026-10-20, Taxi, {amt}, yes")
    assert (r.status_code == 200) is ok
    if not ok:
        assert "no decimals" in r.text or "must be" in r.text  # commas split the line into fields
    else:
        assert web.store.get_claim("CLM-2026-W001")["items"][0]["amount"] == "1350000"


def test_web_status_limited_by_engine(client):
    r = client.get(f"/admin/claims/{HERO}")
    assert '<option value="on_hold">' in r.text and '<option value="approved">' not in r.text
    client.post(f"/admin/claims/{HERO}/status", data={"status": "approved", "reviewer_confirmed": "true"})
    assert web.store.get_claim(HERO)["status"] == "submitted"
    client.post(f"/admin/claims/{HERO}/status", data={"status": "on_hold", "reviewer_confirmed": "true"})
    assert web.store.get_claim(HERO)["status"] == "on_hold"


def test_web_upload_limits(client):
    submit(client, "taxi, 2026-10-20, Taxi, 150000, no")
    base = {"subject": "r", "claim_id": "CLM-2026-W001", "att_type": "receipt", "item_id": "IT-1"}
    assert client.post("/employee/forward", data=base, files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400
    big = b"0" * (web.MAX_UPLOAD_BYTES + 1)
    assert client.post("/employee/forward", data=base, files={"file": ("r.pdf", big, "application/pdf")}).status_code == 413
    assert client.post("/employee/forward", data=base, files={"file": ("r.pdf", b"%PDF", "application/pdf")}).status_code == 200
