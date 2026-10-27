"""Display labels and the code-free promise for finance and employee pages."""
import html
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import plans
from reimburse_web.labels import LABELS, label, person

REQUIRED = {
    "boarding_pass_missing": "Boarding pass belum ada",
    "receipt_missing": "Struk belum ada",
    "hotel_over_cap": "Hotel di atas plafon",
    "hotel_nights_mismatch": "Jumlah malam tidak sesuai tugas",
    "late_checkout_unjustified": "Check-out terlambat tanpa alasan",
    "duplicate_suspected": "Perlu ditinjau finance",
    "approve_full": "Setujui penuh",
    "approve_capped": "Setujui sebatas plafon",
    "reject": "Tolak",
    "needs_info": "Butuh kelengkapan",
    "approve": "Siap disetujui",
    "partial_approve": "Disetujui sebagian",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.delenv("EMPLOYEE_QA", raising=False)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


@pytest.fixture
def pending_plan(tmp_path):
    """A real pending plan in a real PlanStore, as tests/test_web_ui_p3p4.py builds them."""
    from reimburse_agent.audit import JsonlAuditLog
    from reimburse_agent.office import plan as P
    from reimburse_agent.office import rules as R
    from reimburse_agent.office.store import PlanStore

    plans.STATE.update(store=PlanStore(tmp_path), log=JsonlAuditLog())
    intents = P.read_request("Tolong kirim notulen ke Pak Budi ya, terus jadwalin follow-up Jumat siang.",
                             [{"id": "DOC-1", "text": "Rapat mingguan."}])
    plan, _ = P.build_plan(run_id="w1", version=1, request="req", requester="EMP-101",
                           intents=intents, resolved={"Pak Budi": "EMP-202"},
                           now=datetime(2026, 10, 26, 2, tzinfo=timezone.utc), directory=plans.directory(),
                           rules=R.default_rules(), notices=[])
    plans.store().save_plan(plan)
    yield plan
    plans.STATE.clear()


def _visible(markup: str) -> str:
    """Text a reader sees: scripts/styles and every tag (with its attributes) removed."""
    markup = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    markup = re.sub(r"<[^>]+>", " ", markup)
    return re.sub(r"\s+", " ", html.unescape(markup))


def _draft(client) -> str:
    r = client.post("/employee/drafts", data={"emp": "EMP-001", "purpose": "Client visit", "destination": "Surabaya",
                                              "start_date": "2026-10-02", "end_date": "2026-10-04"},
                    follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


def _employee_pages(client) -> dict[str, str]:
    # Populate both mailbox tables so their former ID columns are exercised.
    client.post("/admin/claims/CLM-2026-0101/message",
                data={"subject": "Claim CLM-2026-0101: receipt", "body": "Mohon kirim struk (IT-1)."})
    client.post("/employee/forward", data={"emp": "EMP-001", "subject": "Taxi receipt", "claim_id": "CLM-2026-0101",
                                           "att_type": "receipt", "item_id": "IT-1"})
    did = _draft(client)
    return {
        "/employee": client.get("/employee").text,
        "/employee?emp=EMP-004": client.get("/employee?emp=EMP-004").text,
        "/employee/claims": client.get("/employee/claims").text,
        "/employee/claims/CLM-2026-0101": client.get("/employee/claims/CLM-2026-0101").text,
        "/employee/new": client.get("/employee/new").text,
        f"/employee/drafts/{did}": client.get(f"/employee/drafts/{did}").text,
    }


@pytest.mark.parametrize("code,text", sorted(REQUIRED.items()))
def test_required_label(code, text):
    assert LABELS[code] == text
    assert label(code) == text


def test_label_humanises_unknown_and_is_none_safe():
    assert label("some_brand_new_code") == "Some brand new code"
    assert label("gap") == "Gap"
    assert label(None) == ""
    assert label("") == ""
    assert label(123) == "123"


def test_person_accepts_store_or_list_and_falls_back_to_id():
    assert person(web.store, "EMP-001") == "Rina Wulandari"
    assert person(web.store.employees, "EMP-001") == "Rina Wulandari"
    assert person(web.store, "EMP-999") == "EMP-999"
    assert person([], None) == ""


def test_employee_pages_show_no_ids(client):
    for url, markup in _employee_pages(client).items():
        text = _visible(markup)
        assert not re.search(r"EMP-\d+", text), url
        assert not re.search(r"MSG-\w+", text), url


def test_finance_and_employee_pages_show_no_raw_codes(client, pending_plan):
    pages = {"/admin": client.get("/admin").text,
             "/admin/plans/w1": client.get("/admin/plans/w1").text}
    for claim in web.store.claims:
        pages[f"/admin/claims/{claim['id']}"] = client.get(f"/admin/claims/{claim['id']}").text
    pages.update(_employee_pages(client))
    for url, markup in pages.items():
        codes = sorted(set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", _visible(markup))))
        assert codes == [], (url, codes)


def test_confirmation_page_has_site_nav_and_pending_badge(client, pending_plan):
    markup = client.get("/admin/plans/w1").text
    body = markup.split("<body>", 1)[1]
    assert 'id="nav-pending"' in body.split("</nav>", 1)[0]
    assert re.search(r'id="pending-badge">1<', markup)
    assert 'href="/admin/plans"' in markup


def test_finance_page_shows_names_with_id_tooltips(client):
    markup = client.get("/admin").text
    assert "Rina Wulandari" in markup and "EMP-001" in markup  # ID only in the title attribute
    header = client.get("/admin/claims/CLM-2026-0101").text
    assert 'title="EMP-001"' in header and ">Rina Wulandari<" in header
