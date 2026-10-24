from decimal import Decimal

import pytest

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.models import Decision, ItemStatus
from reimburse_buddy.rules import check_claim
from reimburse_buddy.triage import triage_claims

HERO, EMP = "CLM-2026-0110", "EMP-001"


@pytest.fixture
def store():
    return DataStore()


def it(c, iid):
    return next(i for i in c.items if i.item_id == iid)


def test_hero_starts_with_four_gaps(store):
    c = check_claim(store, HERO)
    assert {g for i in c.items for g in i.gaps} == {
        "boarding_pass_missing", "hotel_over_cap", "late_checkout_unjustified", "receipt_missing"}


def test_boarding_pass_search_dead_end_then_flight_number(store):
    hits = [h["id"] for h in store.search_inbox(EMP, "boarding pass")]
    assert "MSG-5201" not in hits  # the airline says "Check-in successful", not "boarding pass"
    assert it(check_claim(store, HERO, hits), "IT-1").status == ItemStatus.NEEDS_EVIDENCE  # wrong trip's pass
    hits = [h["id"] for h in store.search_inbox(EMP, "QZ 7520")]
    assert hits == ["MSG-5201"]
    assert it(check_claim(store, HERO, hits), "IT-1").status == ItemStatus.OK


def test_unsigned_memo_trap(store):
    memos = store.find_approval_memo(EMP, "Surabaya", "2026-10-02")
    assert [(m["id"], m["signed"]) for m in memos] == [("MEMO-2026-038", False)]
    c = check_claim(store, HERO, ["MEMO-2026-038"])
    assert [(i.id, i.reason) for i in c.ignored_evidence] == [("MEMO-2026-038", "memo_unsigned")]
    assert it(c, "IT-3").gaps == ["hotel_over_cap"]


def test_cross_source_meeting_update(store):
    assert it(check_claim(store, HERO, ["EVT-7010"]), "IT-4").gaps == ["late_checkout_unjustified"]
    c = check_claim(store, HERO, ["EVT-7010", "MSG-5202"])
    assert it(c, "IT-4").status == ItemStatus.OK
    assert it(c, "IT-4").evidence_used == ["EVT-7010", "MSG-5202"]
    # the update alone, without the calendar event, is not enough
    assert it(check_claim(store, HERO, ["MSG-5202"]), "IT-4").status == ItemStatus.NEEDS_EVIDENCE


def test_hero_full_resolution(store):
    store.send_message(EMP, "Please send the taxi receipt (IT-5)", "receipt", HERO)
    ev = ["MSG-5201", "MEMO-2026-038", "EVT-7010", "MSG-5202", "MSG-5203"]
    c = check_claim(store, HERO, ev)
    assert c.decision == Decision.NEEDS_INFO and c.total_pending == Decimal("300000")
    c = check_claim(store, HERO, ev, {"IT-3": "approve_capped"})
    assert c.decision == Decision.PARTIAL
    assert c.total_approvable == Decimal("6590000")


def test_batch_triage(store):
    t = triage_claims(store, "2026-W43")
    assert t["claims"] == 10
    assert len(t["auto_cleared"]) == 7
    assert [e["claim_id"] for e in t["exceptions"]] == ["CLM-2026-0101", "CLM-2026-0104", HERO]
    assert t["time_saved_estimate"]["minutes"] == "105"


def test_demo_hero_and_triage(capsys):
    from reimburse_buddy.demo import main
    assert main(["hero"]) == 0 and main(["triage"]) == 0
    out = capsys.readouterr().out
    assert "memo_unsigned" in out and "7 auto-cleared" in out
