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
    hits = store.search_inbox(EMP, "boarding pass")
    assert hits[0]["id"] == "MSG-5001"  # top hit is the wrong trip's pass; the airline says "Check-in successful"
    c = check_claim(store, HERO, ["MSG-5001"])
    assert it(c, "IT-1").status == ItemStatus.NEEDS_EVIDENCE
    assert [(i.id, i.reason) for i in c.ignored_evidence] == [("MSG-5001", "no_matching_item")]
    for q in ("QZ 7520", "qz7520", "boarding pass QZ7520"):
        assert store.search_inbox(EMP, q)[0]["id"] == "MSG-5201", q
    assert it(check_claim(store, HERO, ["MSG-5201"]), "IT-1").status == ItemStatus.OK


def test_search_is_ranked_or_and_trimmed(store):
    hits = store.search_inbox(EMP, "meeting rescheduled")
    assert hits[0]["id"] == "MSG-5202"
    assert set(hits[0]) == {"id", "date", "from", "subject", "snippet", "attachments", "score"}
    assert len(store.search_inbox(EMP, "")) <= 5


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


def test_indonesian_request_triggers_reply(store):
    out = store.send_message(EMP, "Mohon kirim struk taksi tanggal 3 Okt.", "Klaim CLM-2026-0110", HERO)
    assert out["replies_received"] == 1
    assert store.search_inbox(EMP, "struk taksi kuitansi taxi receipt")[0]["id"] == "MSG-5203"


def test_reset_demo_restores_state(store):
    store.send_message(EMP, "taxi receipt", "", HERO)
    store.update_claim_status("CLM-2026-0111", "approved")
    assert triage_claims(store, "2026-W43")["claims"] == 9
    store.reset()
    assert store.outbox == [] and store.status_log == []
    assert all(m["id"] != "MSG-5203" for m in store.inbox)
    assert triage_claims(store, "2026-W43")["claims"] == 10
    assert store.send_message(EMP, "taxi receipt", "", HERO)["replies_received"] == 1


@pytest.mark.parametrize("ev,hd", [("MSG-5201", None), (None, [["IT-3", "approve_capped"]]),
                                   (None, {"IT-3": "yes"}), (None, {"IT-99": "reject"})])
def test_shape_errors_explain_expected_format(store, ev, hd):
    with pytest.raises(ValueError, match=r'e\.g\.'):
        check_claim(store, HERO, ev, hd)


def test_decision_note_audit_trail(store):
    from reimburse_buddy.decision import draft_decision
    store.send_message(EMP, "taxi receipt", "", HERO)
    ev = ["MSG-5001", "MSG-5201", "MEMO-2026-038", "EVT-7010", "MSG-5202", "MSG-5203"]
    text = draft_decision(store, check_claim(store, HERO, ev, {"IT-3": "approve_capped"}), language="both")
    id_part, en_part = text.split("\n\n---\n\n")
    assert "MEMO-2026-038: memo belum ditandatangani" in id_part
    assert "MEMO-2026-038: memo was never signed" in en_part
    assert "MSG-5001: does not match any item" in en_part
    assert "Sudah diputuskan (setujui sebatas plafon): Rp1.350.000/malam" in id_part
    assert "Resolved (approve_capped): Rp1.350.000/night" in en_part
    for word in ("Receipt", "found in", "exceeds", "night"):
        assert word not in id_part, word
    assert ".;" not in text and "1350000" not in text