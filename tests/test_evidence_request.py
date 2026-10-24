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
    assert list(inspect.signature(req).parameters) == ["store", "claim_id", "item_id", "gap_code"]


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
    assert len(store.outbox) == 1
    req(store, HERO, "IT-5", "receipt_missing")
    assert len(store.outbox) == 2


def test_send_message_unchanged_for_version_a(store):
    out = store.send_message("EMP-001", "any free text Rp1.000.000", "s", HERO)
    assert out["status"] == "sent" and "evidence_request" not in store.outbox[-1]
