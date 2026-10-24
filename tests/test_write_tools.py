import pytest

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.models import Decision, ItemStatus
from reimburse_buddy.rules import check_claim


@pytest.fixture
def store():
    return DataStore()  # fresh in-memory state per test


def test_send_message_triggers_scripted_reply_and_closes_loop(store):
    assert store.search_inbox("EMP-003", "taxi receipt") == []
    out = store.send_message("EMP-003", "Please send the second taxi receipt (IT-6).", "taxi", "CLM-2026-0104")
    assert out == {"status": "sent", "message_id": "OUT-001", "replies_received": 1}
    hits = store.search_inbox("EMP-003", "taxi receipt")
    assert [h["id"] for h in hits] == ["MSG-5101"]
    c = check_claim(store, "CLM-2026-0104", ["EVT-7004", "MSG-5101"])
    it6 = next(i for i in c.items if i.item_id == "IT-6")
    assert it6.status == ItemStatus.OK and "MSG-5101" in it6.evidence_used
    assert c.decision == Decision.PARTIAL  # only the post-trip dinner is rejected


def test_reply_is_delivered_once_and_only_on_match(store):
    assert store.send_message("EMP-003", "Hello", "", "CLM-2026-0104")["replies_received"] == 0
    assert store.send_message("EMP-003", "taxi", "", "CLM-2026-0104")["replies_received"] == 1
    assert store.send_message("EMP-003", "taxi again", "", "CLM-2026-0104")["replies_received"] == 0
    assert len(store.outbox) == 3


def test_request_approval_checks_manager(store):
    req = store.request_approval("EMP-100", "CLM-2026-0103", "IT-1", "Hotel over cap, no memo")
    assert req["status"] == "pending" and req["id"] == "APR-001"
    with pytest.raises(ValueError):
        store.request_approval("EMP-101", "CLM-2026-0103", "IT-1")
    with pytest.raises(KeyError):
        store.request_approval("EMP-100", "CLM-2026-0103", "IT-99")


def test_update_claim_status(store):
    e = store.update_claim_status("CLM-2026-0101", "approved", "all evidence found")
    assert e["from"] == "submitted" and store.get_claim("CLM-2026-0101")["status"] == "approved"
    with pytest.raises(ValueError):
        store.update_claim_status("CLM-2026-0101", "paid")
