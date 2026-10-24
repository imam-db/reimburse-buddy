"""Regression tests for the review fixes (PR1)."""

from decimal import Decimal

from conftest import claim
from reimburse_buddy.models import Decision, ItemStatus
from reimburse_buddy.rules import check_claim


def it(check, iid):
    return next(i for i in check.items if i.item_id == iid)


def flight(iid, d, no, leg, bp=True, rno=None):
    return {"id": iid, "category": "flight", "date": d, "description": no, "amount": "1000000",
            "flight_no": no, "leg": leg, "receipt": True, "receipt_no": rno or f"T-{iid}", "boarding_pass": bp}


# (1) a hotel without a receipt is not approvable, even the capped part
def test_hotel_missing_receipt_counts_nothing(make_store):
    s = make_store(claims=[claim("T-1", [{"id": "H", "category": "hotel", "date": "2026-10-12",
                                         "amount": "3000000", "nights": 2, "receipt": False}])])
    c = check_claim(s, "T-1")
    assert set(it(c, "H").gaps) == {"receipt_missing", "hotel_over_cap"}
    assert c.total_approvable == 0 and c.total_pending == Decimal("3000000")


# (2) human decisions work for every category
def test_human_decision_for_late_checkout_and_flight(make_store):
    s = make_store(claims=[claim("T-2", [
        flight("F", "2026-10-12", "GA 1", "outbound", bp=False), flight("R", "2026-10-14", "GA 2", "return"),
        {"id": "L", "category": "late_checkout", "date": "2026-10-14", "amount": "800000",
         "checkout_time": "16:00", "location": "Surabaya", "receipt": True}])])
    c = check_claim(s, "T-2", [], {"F": "approve_full", "L": "approve_capped"})
    assert it(c, "F").approvable == Decimal("1000000")
    assert it(c, "L").approvable == Decimal("600000")  # 50% of tier-1 cap
    assert c.decision == Decision.PARTIAL


# (3) hotel nights must fit inside the trip
def test_hotel_nights_beyond_trip(make_store):
    s = make_store(claims=[claim("T-3", [{"id": "H", "category": "hotel", "date": "2026-10-13",
                                         "amount": "2000000", "nights": 2, "receipt": True}])])
    assert it(check_claim(s, "T-3"), "H").gaps == ["hotel_nights_mismatch"]


# (4) duplicates: same receipt number rejects, distinct numbers pass, missing numbers are suspected
def test_duplicate_by_receipt_number(make_store):
    taxi = lambda iid, rno: {"id": iid, "category": "taxi", "date": "2026-10-13", "amount": "100000",
                             "receipt": True, **({"receipt_no": rno} if rno else {})}
    s = make_store(claims=[claim("T-4", [taxi("A", "X1"), taxi("B", "X1"), taxi("C", "X2"), taxi("D", None)])])
    c = check_claim(s, "T-4")
    assert it(c, "B").status == ItemStatus.REJECTED
    assert it(c, "C").status == ItemStatus.OK
    assert it(c, "D").gaps == ["duplicate_suspected"]


# (5) boarding pass must match date, flight number and passenger
def test_boarding_pass_must_match_flight_and_passenger(make_store):
    msg = lambda mid, fl, who: {"id": mid, "owner_id": "EMP-001", "from": "a@x.example", "date": "2026-10-11",
                                "subject": "bp", "body": "", "attachments": [
                                    {"name": "bp.pdf", "type": "boarding_pass", "flight": fl, "date": "2026-10-12", "passenger": who}]}
    s = make_store(claims=[claim("T-5", [flight("F", "2026-10-12", "GA 312", "outbound", bp=False),
                                         flight("R", "2026-10-14", "GA 317", "return")])],
                   inbox=[msg("M-W1", "GA 999", "Rina Wulandari"), msg("M-W2", "GA 312", "Budi Santoso"),
                          msg("M-OK", "ga312", "RINA WULANDARI")])
    assert it(check_claim(s, "T-5", ["M-W1", "M-W2"]), "F").status == ItemStatus.NEEDS_EVIDENCE
    assert it(check_claim(s, "T-5", ["M-OK"]), "F").status == ItemStatus.OK


# (6) outbound flight without a return flight gives a warning
def test_return_flight_warning(make_store):
    s = make_store(claims=[claim("T-6", [flight("F", "2026-10-12", "GA 1", "outbound")])])
    c = check_claim(s, "T-6")
    assert [w.gap for w in c.warnings] == ["return_flight_missing"]
    assert c.decision == Decision.APPROVE  # a warning does not block payment


# (7) late check-out: meeting location and requested time must fit
def test_late_checkout_location_and_time(make_store):
    ev = lambda eid, loc, end: {"id": eid, "owner_id": "EMP-001", "title": "m", "date": "2026-10-14",
                                "start": "13:00", "end": end, "location": loc, "type": "business"}
    lc = {"id": "L", "category": "late_checkout", "date": "2026-10-14", "amount": "300000",
          "checkout_time": "18:00", "location": "Surabaya", "receipt": True}
    s = make_store(claims=[claim("T-7", [lc])],
                   calendar=[ev("E-JKT", "Jakarta", "17:00"), ev("E-EARLY", "Surabaya", "15:00"), ev("E-OK", "Surabaya", "16:30")])
    assert it(check_claim(s, "T-7", ["E-JKT"]), "L").gaps == ["late_checkout_unjustified"]
    assert it(check_claim(s, "T-7", ["E-EARLY"]), "L").gaps == ["late_checkout_unjustified"]  # 18:00 > 15:00 + 2h
    assert it(check_claim(s, "T-7", ["E-OK"]), "L").status == ItemStatus.OK


def test_receipt_from_inbox_resolves_missing_receipt(make_store):
    s = make_store(claims=[claim("T-8", [{"id": "X", "category": "taxi", "date": "2026-10-13", "amount": "90000", "receipt": False}])],
                   inbox=[{"id": "M-R", "owner_id": "EMP-001", "from": "a", "date": "2026-10-15", "subject": "receipt",
                           "body": "", "attachments": [{"name": "r.jpg", "type": "receipt", "claim_id": "T-8", "item_id": "X", "receipt_no": "R-1"}]}])
    assert check_claim(s, "T-8").decision == Decision.NEEDS_INFO
    assert check_claim(s, "T-8", ["M-R"]).decision == Decision.APPROVE
