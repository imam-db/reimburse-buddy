"""Meeting updates must be applied by message date, not by evidence_ids order."""

from conftest import claim
from reimburse_buddy.models import ItemStatus
from reimburse_buddy.rules import check_claim


def it(check, item_id):
    return next(i for i in check.items if i.item_id == item_id)


def late_checkout_item():
    return {"id": "IT-LC", "category": "late_checkout", "date": "2026-10-14", "amount": "300000",
            "checkout_time": "16:00", "location": "Surabaya", "receipt": True}


def business_event(event_id="EVT-9001", start="10:00", end="12:00"):
    return {"id": event_id, "owner_id": "EMP-001", "title": "Business meeting", "date": "2026-10-14",
            "start": start, "end": end, "location": "Surabaya", "type": "business"}


def meeting_update(message_id, message_date, start, end, event_id="EVT-9001"):
    return {"id": message_id, "owner_id": "EMP-001", "from": "manager@example.com",
            "date": message_date, "subject": "Meeting update", "body": "", "attachments": [],
            "meeting_update": {"event_id": event_id, "date": "2026-10-14", "start": start,
                               "end": end, "location": "Surabaya"}}


def lc_finding(check):
    return next(f for f in it(check, "IT-LC").findings if f.key == "lc_ok")


def test_latest_dated_update_wins_regardless_of_evidence_order(make_store):
    store = make_store(
        claims=[claim("CLM-ORDER", [late_checkout_item()])],
        calendar=[business_event()],
        inbox=[
            meeting_update("MSG-A", "2026-10-12", "15:00", "16:30"),
            meeting_update("MSG-B", "2026-10-13", "10:00", "12:00"),
        ],
    )

    for evidence_ids in (["EVT-9001", "MSG-B", "MSG-A"], ["EVT-9001", "MSG-A", "MSG-B"]):
        result = it(check_claim(store, "CLM-ORDER", evidence_ids), "IT-LC")
        assert result.status == ItemStatus.NEEDS_EVIDENCE
        assert result.gaps == ["late_checkout_unjustified"]


def test_single_update_justifies_and_is_not_calendar_only(make_store):
    store = make_store(
        claims=[claim("CLM-UPDATE", [late_checkout_item()])],
        calendar=[business_event()],
        inbox=[meeting_update("MSG-A", "2026-10-12", "15:00", "16:30")],
    )

    check = check_claim(store, "CLM-UPDATE", ["EVT-9001", "MSG-A"])
    result = it(check, "IT-LC")
    assert result.status == ItemStatus.OK
    assert result.evidence_used == ["EVT-9001", "MSG-A"]
    assert lc_finding(check).params["calendar_only"] is False


def test_calendar_alone_justifies_and_is_calendar_only(make_store):
    store = make_store(
        claims=[claim("CLM-CALENDAR", [late_checkout_item()])],
        calendar=[business_event(start="13:00", end="15:00")],
    )

    check = check_claim(store, "CLM-CALENDAR", ["EVT-9001"])
    result = it(check, "IT-LC")
    assert result.status == ItemStatus.OK
    assert result.evidence_used == ["EVT-9001"]
    assert lc_finding(check).params["calendar_only"] is True
