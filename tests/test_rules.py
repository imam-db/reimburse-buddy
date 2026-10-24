from decimal import Decimal

import pytest

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.decision import draft_decision
from reimburse_buddy.models import Decision, ItemStatus
from reimburse_buddy.rules import check_claim


@pytest.fixture(scope="module")
def store():
    return DataStore()


def item(check, item_id):
    return next(i for i in check.items if i.item_id == item_id)


# Scenario 1: missing boarding pass, found in the inbox
def test_boarding_pass_missing_then_found(store):
    c = check_claim(store, "CLM-2026-0101")
    assert c.decision == Decision.NEEDS_INFO
    assert item(c, "IT-1").status == ItemStatus.NEEDS_EVIDENCE
    assert c.total_pending == Decimal("1450000")
    assert item(c, "IT-1").gaps == ["boarding_pass_missing"]
    hits = store.search_inbox("EMP-001", "boarding pass")
    assert hits[0]["id"] == "MSG-5001"
    c = check_claim(store, "CLM-2026-0101", [h["id"] for h in hits])
    assert c.decision == Decision.APPROVE
    assert c.total_approvable == Decimal("6265000")


def test_other_employees_evidence_is_ignored(store):
    c = check_claim(store, "CLM-2026-0101", ["MSG-5004", "NOPE-1"])
    assert [(i.id, i.reason) for i in c.ignored_evidence] == [
        ("MSG-5004", "belongs_to_other_employee"), ("NOPE-1", "unknown_id")]
    assert c.decision == Decision.NEEDS_INFO


# Scenario 2a: hotel over cap, signed memo found
def test_hotel_over_cap_with_signed_memo(store):
    c = check_claim(store, "CLM-2026-0102")
    assert item(c, "IT-2").status == ItemStatus.NEEDS_EVIDENCE
    assert c.total_approvable == Decimal("6990000")  # capped hotel 2.4M is certain
    assert c.total_pending == Decimal("900000")
    memos = store.find_approval_memo("EMP-002", "Jakarta", "2026-10-14")
    assert [m["id"] for m in memos] == ["MEMO-2026-041"]
    c = check_claim(store, "CLM-2026-0102", ["MEMO-2026-041"])
    assert c.decision == Decision.APPROVE
    assert c.total_approvable == Decimal("7890000")


def test_unsigned_memo_does_not_count(store):
    c = check_claim(store, "CLM-2026-0102", ["MEMO-2026-038"])
    assert [i.reason for i in c.ignored_evidence] == ["belongs_to_other_employee"]
    c = check_claim(store, "CLM-2026-0101", ["MEMO-2026-038"])
    assert [i.reason for i in c.ignored_evidence] == ["memo_unsigned"]
    assert c.decision == Decision.NEEDS_INFO


# Scenario 2b: hotel over cap, no memo, human decides
@pytest.mark.parametrize("choice,approved,decision", [
    ("approve_capped", "1500000", Decision.PARTIAL),
    ("approve_full", "1750000", Decision.APPROVE),
    ("reject", "700000", Decision.PARTIAL),
])
def test_hotel_over_cap_human_decision(store, choice, approved, decision):
    assert store.find_approval_memo("EMP-002", "Medan", "2026-10-20") == []
    c = check_claim(store, "CLM-2026-0103", [], {"IT-1": choice})
    assert c.total_approvable == Decimal(approved)
    assert c.decision == decision


def test_invalid_human_choice(store):
    with pytest.raises(ValueError):
        check_claim(store, "CLM-2026-0103", [], {"IT-1": "maybe"})


# Scenario 3: late check-out justified by calendar, plus duplicate and out-of-trip items
def test_late_checkout_justified_by_calendar(store):
    c = check_claim(store, "CLM-2026-0104")
    assert item(c, "IT-3").status == ItemStatus.NEEDS_EVIDENCE
    assert item(c, "IT-6").gaps == ["duplicate_suspected"]  # no receipt number: suspected, not rejected
    assert item(c, "IT-7").status == ItemStatus.REJECTED  # after the trip
    c = check_claim(store, "CLM-2026-0104", ["EVT-7004", "EVT-7005"])
    assert item(c, "IT-3").status == ItemStatus.OK
    assert item(c, "IT-3").evidence_used == ["EVT-7004"]
    assert c.decision == Decision.NEEDS_INFO
    c = check_claim(store, "CLM-2026-0104", ["EVT-7004"], {"IT-6": "reject"})
    assert c.decision == Decision.PARTIAL
    assert c.total_approvable == Decimal("7190000")


def test_travel_event_does_not_justify_late_checkout(store):
    c = check_claim(store, "CLM-2026-0104", ["EVT-7005"])
    assert item(c, "IT-3").status == ItemStatus.NEEDS_EVIDENCE


def test_draft_decision_mentions_totals(store):
    text = draft_decision(store, check_claim(store, "CLM-2026-0101", ["MSG-5001"]))
    assert "APPROVED" in text and "Rp6.265.000" in text and "MSG-5001" in text


def test_demo_cli_runs(capsys):
    from reimburse_buddy.demo import main
    assert main(["all"]) == 0
    assert "PARTIALLY APPROVED" in capsys.readouterr().out
