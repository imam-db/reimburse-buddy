import json

import pytest

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.rules import check_claim
from reimburse_buddy.triage import approve_cleared_batch, triage_claims

B = "2026-W43"
MAILBOX = "reimburse@nusantara-niaga.example"


@pytest.fixture
def store():
    return DataStore()


def statuses(store):
    return {c["id"]: c["status"] for c in store.claims}


def test_triage_never_changes_status(store):
    before = statuses(store)
    t = triage_claims(store, B)
    assert (t["claims"], len(t["auto_cleared"]), len(t["exceptions"])) == (10, 7, 3)
    assert statuses(store) == before and store.status_log == []


def test_bulk_approve_requires_confirmation(store):
    cleared = triage_claims(store, B)["auto_cleared"]
    res = approve_cleared_batch(store, B, cleared, reviewer_confirmed=False)
    assert res["ok"] is False and "reviewer_confirmed" in res["error"]
    assert store.status_log == []


def test_bulk_approve_requires_exact_list(store):
    cleared = triage_claims(store, B)["auto_cleared"]
    res = approve_cleared_batch(store, B, cleared[:-1] + ["CLM-2026-0110"], reviewer_confirmed=True)
    assert res["ok"] is False
    assert res["not_in_your_list"] == [cleared[-1]] and res["not_cleared"] == ["CLM-2026-0110"]
    assert store.status_log == []
    assert approve_cleared_batch(store, B, "CLM-2026-0111", True)["ok"] is False  # wrong shape


def test_bulk_approve_happy_path(store):
    cleared = triage_claims(store, B)["auto_cleared"]
    res = approve_cleared_batch(store, B, list(reversed(cleared)), reviewer_confirmed=True)
    assert res == {"ok": True, "approved": cleared, "count": 7}
    assert all(store.get_claim(c)["status"] == "approved" for c in cleared)
    assert store.get_claim("CLM-2026-0110")["status"] == "submitted"
    # second call: nothing is cleared any more
    assert approve_cleared_batch(store, B, cleared, True)["ok"] is False


def test_all_mail_lives_in_the_shared_mailbox(store):
    assert all(m["mailbox"] == MAILBOX for m in store.inbox)
    store.send_message("EMP-001", "Mohon kirim struk taksi (IT-5)", "Klaim CLM-2026-0110", "CLM-2026-0110")
    assert all(m["mailbox"] == MAILBOX for m in store.inbox)
    assert store.search_inbox is not None  # alias kept for compatibility
    assert store.search_reimburse_mailbox("EMP-001", "taxi receipt")[0]["id"] == "MSG-5203"


def test_late_checkout_gap_states_facts_only(store):
    f = next(f for i in check_claim(store, "CLM-2026-0110").items for f in i.findings if f.gap == "late_checkout_unjustified")
    text = f.message.lower()
    assert "16:30" in text and "needs justification" in text
    for hint in ("email", "mail", "inbox", "moved", "reschedul", "search", "meeting"):
        assert hint not in text, hint
