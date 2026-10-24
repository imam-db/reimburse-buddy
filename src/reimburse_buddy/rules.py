"""Deterministic rule engine. The LLM never does arithmetic; it calls these functions.

check_claim() takes the claim plus the evidence IDs the agent has found
(inbox messages, calendar events, approval memos) and returns a per-item verdict.
Each unresolved problem is reported as a gap code (see gaps.py), not a tool call.
human_decisions ({item_id: approve_full | approve_capped | reject}) let a reviewer
settle any item the rules cannot decide, whatever its category.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from . import gaps as G
from .data_store import DataStore
from .models import ClaimCheck, Decision, Finding, IgnoredEvidence, ItemResult, ItemStatus

HUMAN_CHOICES = {"approve_full", "approve_capped", "reject"}
NEEDS = ItemStatus.NEEDS_EVIDENCE


def _d(x) -> Decimal:
    return Decimal(str(x))


def city_tier(policy: dict, city: str) -> str:
    return str(policy["city_tiers"].get(city, policy["default_tier"]))


def trip_days(trip: dict) -> int:
    return (date.fromisoformat(trip["end_date"]) - date.fromisoformat(trip["start_date"])).days + 1


def _t(s: str):
    return datetime.strptime(s, "%H:%M")


def _norm(s: str | None) -> str:
    return "".join((s or "").lower().split())


def _resolve_evidence(store: DataStore, emp: str, evidence_ids):
    evidence: dict[str, tuple[str, dict]] = {}
    ignored: list[IgnoredEvidence] = []
    for eid in evidence_ids or []:
        found = store.find_evidence(eid)
        if not found:
            ignored.append(IgnoredEvidence(eid, "unknown_id"))
            continue
        kind, row = found
        owners = row.get("employee_ids") or [row.get("owner_id")]
        if emp not in owners:
            ignored.append(IgnoredEvidence(eid, "belongs_to_other_employee"))
        elif kind == "memo" and not row.get("signed"):
            ignored.append(IgnoredEvidence(eid, "memo_unsigned"))
        else:
            evidence[eid] = (kind, row)
    return evidence, ignored


def _attachments(evidence, kind="inbox"):
    for eid, (k, row) in evidence.items():
        if k == kind:
            for a in row.get("attachments", []):
                yield eid, a


def _meetings(evidence) -> list[tuple[list[str], dict]]:
    """Calendar events, with time/location updates from inbox messages applied."""
    events = {eid: ([eid], dict(row)) for eid, (k, row) in evidence.items() if k == "calendar"}
    for mid, (k, row) in evidence.items():
        upd = row.get("meeting_update") if k == "inbox" else None
        if upd and upd["event_id"] in events:
            ids, ev = events[upd["event_id"]]
            ev.update({key: upd[key] for key in ("date", "start", "end", "location") if key in upd})
            ids.append(mid)
    return list(events.values())


def check_claim(
    store: DataStore,
    claim_id: str,
    evidence_ids: list[str] | None = None,
    human_decisions: dict[str, str] | None = None,
) -> ClaimCheck:
    claim = store.get_claim(claim_id)
    policy = store.policy
    emp = claim["employee_id"]
    emp_name = store.get_employee(emp)["name"]
    trip = claim["trip"]
    tier = city_tier(policy, trip["destination"])
    cap = _d(policy["hotel_nightly_cap"][tier])
    human_decisions = dict(human_decisions or {})
    for item_id, choice in human_decisions.items():
        if choice not in HUMAN_CHOICES:
            raise ValueError(f"human_decisions[{item_id}] must be one of {sorted(HUMAN_CHOICES)}")

    evidence, ignored = _resolve_evidence(store, emp, evidence_ids)
    receipts = {(a.get("claim_id"), a.get("item_id")): (eid, a)
                for eid, a in _attachments(evidence) if a.get("type") == "receipt"}

    # Effective receipt numbers (from the claim or from receipts found in the inbox).
    receipt_no: dict[str, str | None] = {}
    for item in claim["items"]:
        found = receipts.get((claim_id, item["id"]))
        receipt_no[item["id"]] = item.get("receipt_no") or (found[1].get("receipt_no") if found else None)

    results: list[ItemResult] = []
    seen_receipts: dict[str, str] = {}
    seen_keys: dict[tuple, str] = {}

    for item in claim["items"]:
        cat, iid = item["category"], item["id"]
        amount = _d(item["amount"])
        res = ItemResult(iid, cat, amount, amount, ItemStatus.OK)
        allowed = amount  # what the rules would pay once all gaps are closed
        rejected = False

        def gap(rule, code, msg):
            res.findings.append(Finding(rule, NEEDS, msg, code))
            res.gaps.append(code)

        # Dates within trip
        if not (trip["start_date"] <= item["date"] <= trip["end_date"]):
            res.findings.append(Finding("dates_within_trip", ItemStatus.REJECTED,
                f"Expense date {item['date']} is outside the trip {trip['start_date']}..{trip['end_date']}."))
            rejected = True

        # Receipt required (a receipt found in the inbox counts)
        if cat in policy["receipt_required_categories"] and not item.get("receipt"):
            found = receipts.get((claim_id, iid))
            if found:
                res.evidence_used.append(found[0])
                res.findings.append(Finding("receipt_required", ItemStatus.OK, f"Receipt found in {found[0]}."))
            else:
                gap("receipt_required", G.RECEIPT_MISSING, "Receipt missing.")

        # Duplicates: same receipt number is a hard duplicate; same category/date/amount
        # without distinct receipt numbers is only suspected.
        rno = receipt_no[iid]
        if rno and not item.get("receipt_no"):
            src = receipts[(claim_id, iid)][0]
            if src not in res.evidence_used:
                res.evidence_used.append(src)
        key = (cat, item["date"], amount)
        if rno and rno in seen_receipts:
            res.findings.append(Finding("duplicate", ItemStatus.REJECTED,
                f"Same receipt number {rno} as {seen_receipts[rno]}."))
            rejected = True
        elif key in seen_keys:
            other = seen_keys[key]
            if not (rno and receipt_no[other]):
                gap("duplicate", G.DUPLICATE_SUSPECTED,
                    f"Same category, date and amount as {other} and no distinct receipt number.")
        if rno:
            seen_receipts.setdefault(rno, iid)
        seen_keys.setdefault(key, iid)

        # Flights: boarding pass must match date, flight number and passenger
        if cat in policy["boarding_pass_required_for"] and not item.get("boarding_pass"):
            match = next((eid for eid, a in _attachments(evidence) if a.get("type") == "boarding_pass"
                          and a.get("date") == item["date"]
                          and _norm(a.get("flight")) == _norm(item.get("flight_no"))
                          and _norm(a.get("passenger")) == _norm(emp_name)), None)
            if match:
                res.evidence_used.append(match)
                res.findings.append(Finding("boarding_pass_required", ItemStatus.OK,
                    f"Boarding pass for {item.get('flight_no')} on {item['date']} ({emp_name}) found in {match}."))
            else:
                gap("boarding_pass_required", G.BOARDING_PASS_MISSING,
                    f"No boarding pass matching flight {item.get('flight_no')}, {item['date']}, passenger {emp_name}.")

        # Hotel: nights inside the trip, nightly cap by tier or signed memo
        if cat == "hotel":
            nights = int(item.get("nights", 1))
            checkout = date.fromisoformat(item["date"]) + timedelta(days=nights)
            if nights < 1 or checkout > date.fromisoformat(trip["end_date"]):
                gap("hotel_nights_within_trip", G.HOTEL_NIGHTS_MISMATCH,
                    f"{nights} night(s) from {item['date']} ends {checkout}, after the trip end {trip['end_date']}.")
            per_night = amount / nights
            if per_night <= cap:
                res.findings.append(Finding("hotel_nightly_cap", ItemStatus.OK, f"{per_night}/night within cap {cap}."))
            else:
                memo = next((eid for eid, (k, r) in evidence.items() if k == "memo"
                             and r["type"] == "hotel_cap_exception"
                             and r["city"].lower() == trip["destination"].lower()
                             and r["valid_from"] <= item["date"] <= r["valid_to"]), None)
                if memo:
                    memo_cap = _d(evidence[memo][1]["approved_nightly_cap"])
                    res.evidence_used.append(memo)
                    allowed = min(amount, memo_cap * nights)
                    res.findings.append(Finding("hotel_nightly_cap",
                        ItemStatus.OK if allowed == amount else ItemStatus.PARTIAL,
                        f"{per_night}/night exceeds tier-{tier} cap {cap}; signed memo {memo} allows {memo_cap}/night."))
                else:
                    allowed = cap * nights
                    gap("hotel_nightly_cap", G.HOTEL_OVER_CAP,
                        f"{per_night}/night exceeds tier-{tier} cap {cap}/night for {trip['destination']}.")

        # Per diem
        if cat == "per_diem":
            limit = _d(policy["per_diem_daily"][tier]) * trip_days(trip)
            allowed = min(amount, limit)
            res.findings.append(Finding("per_diem", ItemStatus.OK if allowed == amount else ItemStatus.PARTIAL,
                f"Allowance {limit} ({trip_days(trip)} days x tier-{tier} rate)."))

        # Late check-out: a business meeting at the hotel's city, ending after the standard
        # check-out time, and the requested check-out not too long after the meeting ends.
        if cat == "late_checkout":
            lc = policy["late_checkout"]
            std = _t(lc["standard_checkout_time"])
            requested = _t(item.get("checkout_time", lc["standard_checkout_time"]))
            buffer = timedelta(hours=float(lc["max_hours_after_meeting"]))
            location = item.get("location", trip["destination"])
            allowed = min(amount, cap * _d(lc["max_fee_ratio_of_nightly_cap"]))
            ok = next((ids for ids, ev in _meetings(evidence) if ev["type"] == "business"
                       and ev["date"] == item["date"] and ev["location"].lower() == location.lower()
                       and _t(ev["end"]) > std and requested <= _t(ev["end"]) + buffer), None)
            if ok:
                res.evidence_used += ok
                res.findings.append(Finding("late_checkout",
                    ItemStatus.OK if allowed == amount else ItemStatus.PARTIAL,
                    f"Check-out {item.get('checkout_time')} justified by business meeting ({', '.join(ok)}) in {location}."))
            else:
                gap("late_checkout", G.LATE_CHECKOUT_UNJUSTIFIED,
                    f"No business meeting in {location} on {item['date']} ending after "
                    f"{lc['standard_checkout_time']} and within {lc['max_hours_after_meeting']}h of check-out {item.get('checkout_time')}.")

        # Combine
        choice = human_decisions.get(iid)
        if rejected or choice == "reject":
            res.status, res.approvable = ItemStatus.REJECTED, Decimal(0)
            if choice == "reject" and not rejected:
                res.findings.append(Finding("human_decision", ItemStatus.REJECTED, "Rejected by a human reviewer."))
        elif res.gaps and choice in ("approve_full", "approve_capped"):
            res.human_decision = choice
            res.approvable = amount if choice == "approve_full" else allowed
            res.status = ItemStatus.OK if res.approvable == amount else ItemStatus.PARTIAL
            res.findings.append(Finding("human_decision", res.status,
                f"Open gaps ({', '.join(res.gaps)}) resolved by a human reviewer: {choice}."))
        elif res.gaps:
            res.status = NEEDS
            # Only the hotel cap leaves a certain amount; any other gap blocks the whole item.
            res.approvable = allowed if set(res.gaps) == {G.HOTEL_OVER_CAP} else Decimal(0)
        else:
            res.approvable = allowed
            res.status = ItemStatus.OK if allowed == amount else ItemStatus.PARTIAL
        if choice == "reject":
            res.human_decision = choice
        results.append(res)

    # Claim-level: an outbound flight should have a return flight
    warnings: list[Finding] = []
    legs = {i.get("leg") for i in claim["items"] if i["category"] == "flight"}
    if "outbound" in legs and "return" not in legs:
        warnings.append(Finding("return_flight", NEEDS,
            "Outbound flight claimed but no return flight. Confirm how the employee returned.", G.RETURN_FLIGHT_MISSING))

    total_claimed = sum((r.claimed for r in results), Decimal(0))
    total_ok = sum((r.approvable for r in results), Decimal(0))
    pending = sum((r.claimed - r.approvable for r in results if r.status == NEEDS), Decimal(0))

    statuses = {r.status for r in results}
    if NEEDS in statuses:
        decision = Decision.NEEDS_INFO
    elif statuses == {ItemStatus.OK}:
        decision = Decision.APPROVE
    elif total_ok == 0:
        decision = Decision.REJECT
    else:
        decision = Decision.PARTIAL

    return ClaimCheck(claim_id, emp, total_claimed, total_ok, pending, decision, results, ignored, warnings)
