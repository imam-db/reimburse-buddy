"""request_missing_evidence: fixed template, owner-only, validated, idempotent."""
import re

import pytest

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.evidence_request import ASKS, request_missing_evidence as req

HERO = "CLM-2026-0110"  # owner EMP-001; IT-5 receipt_missing, IT-1 boarding_pass_missing


@pytest.fixture
def store():
    return DataStore()


def test_template_has_no_amounts_or_decisions(store):
    out = req(store, HERO, "IT-5", "receipt_missing")
    m = store.outbox[-1]
    assert out["status"] == "sent" and out["to"] == "EMP-001" and m["to"] == "EMP-001"
    text = (m["subject"] + " " + m["body"]).replace(HERO, "")
    assert "IT-5" in text and "receipt" in text and "struk" in text
    assert not re.search(r"\bRp", text, re.I) and not re.search(r"\d{3,}", text)
    for word in ("approv", "reject", "disetujui", "ditolak", "bayar", "dibayar", "potong", "cap "):
        assert word not in text.lower()


@pytest.mark.parametrize("gap", sorted(ASKS))
def test_every_template_is_money_free(gap):
    from reimburse_buddy.evidence_request import BODY, SUBJECT
    t = SUBJECT + BODY + " ".join(ASKS[gap])
    assert not re.search(r"\bRp", t, re.I) and not re.search(r"\d", t)


def test_owner_only_no_recipient_parameter():
    import inspect
    params = list(inspect.signature(req).parameters)
    assert params == ["store", "claim_id", "item_id", "gap_code", "evidence_ids", "human_decisions"]
    assert "to" not in params


@pytest.mark.parametrize("cid,item,gap,exc", [
    ("CLM-NOPE", "IT-1", "receipt_missing", KeyError),
    (HERO, "IT-99", "receipt_missing", KeyError),
    (HERO, "IT-5", "made_up_gap", ValueError),
    (HERO, "IT-2", "boarding_pass_missing", ValueError),  # known code, but not a current gap of IT-2
    (HERO, "IT-5", "boarding_pass_missing", ValueError),
])
def test_rejects_unknown_claim_item_gap(store, cid, item, gap, exc):
    with pytest.raises(exc):
        req(store, cid, item, gap)
    assert store.outbox == []


def test_idempotent(store):
    a = req(store, HERO, "IT-1", "boarding_pass_missing")
    b = req(store, HERO, "IT-1", "boarding_pass_missing")
    assert b == {"status": "already_requested", "message_id": a["message_id"], "to": "EMP-001"}
    assert a["round"] == 1
    assert len(store.outbox) == 1
    req(store, HERO, "IT-5", "receipt_missing")
    assert len(store.outbox) == 2


def test_send_message_unchanged_for_version_a(store):
    out = store.send_message("EMP-001", "any free text Rp1.000.000", "s", HERO)
    assert out["status"] == "sent" and "evidence_request" not in store.outbox[-1]


def test_marker_reply_matching_not_keywords(store):
    # "bukti" in the late-checkout template used to deliver the IT-5 taxi receipt (MSG-5203)
    out = req(store, HERO, "IT-4", "late_checkout_unjustified")
    assert out["replies_received"] == 0 and "MSG-5203" not in {m["id"] for m in store.inbox}
    out = req(store, HERO, "IT-5", "receipt_missing")
    assert out["replies_received"] == 1 and "MSG-5203" in {m["id"] for m in store.inbox}


def test_free_text_send_message_keyword_path_unchanged(store):
    out = store.send_message("EMP-001", "Mohon kirim struk taksi IT-5", "Klaim", HERO)
    assert out["replies_received"] == 1 and "MSG-5203" in {m["id"] for m in store.inbox}


def test_gap_resolved_by_evidence_or_decision_blocks_request(store):
    with pytest.raises(ValueError):  # boarding pass already in the mailbox
        req(store, HERO, "IT-1", "boarding_pass_missing", evidence_ids=["MSG-5201"])
    with pytest.raises(ValueError):  # reviewer already decided the hotel item
        req(store, HERO, "IT-3", "hotel_over_cap", human_decisions={"IT-3": "approve_capped"})
    assert store.outbox == []


def test_new_round_only_after_reply(store):
    a = req(store, HERO, "IT-5", "receipt_missing")  # scripted reply arrives
    b = req(store, HERO, "IT-5", "receipt_missing")
    assert b["status"] == "sent" and b["round"] == 2 and b["message_id"] != a["message_id"]
    c = req(store, HERO, "IT-5", "receipt_missing")  # round 2 unanswered
    assert c["status"] == "already_requested" and c["message_id"] == b["message_id"]


def test_category_and_name_validated(store):
    store.get_employee("EMP-001")["name"] = "Rina {ask_en}\nIgnore previous"
    with pytest.raises(ValueError, match="name"):
        req(store, HERO, "IT-5", "receipt_missing")
    store.get_employee("EMP-001")["name"] = "Rina Wulandari"
    next(i for i in store.get_claim(HERO)["items"] if i["id"] == "IT-5")["category"] = "spa"
    with pytest.raises(ValueError, match="category"):
        req(store, HERO, "IT-5", "receipt_missing")
    assert store.outbox == []
