"""Deterministic rule engine. The LLM never does arithmetic; it calls these functions.

check_claim() takes the claim plus the evidence IDs the agent has found
(inbox messages, calendar events, approval memos) and returns a per-item verdict.
Optional human_decisions let a reviewer resolve items the rules cannot decide.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from .data_store import DataStore
from .models import ClaimCheck, Decision, Finding, ItemResult, ItemStatus

HUMAN_CHOICES = {"approve_full", "approve_capped", "reject"}


def _d(x) -> Decimal:
    return Decimal(str(x))


def city_tier(policy: dict, city: str) -> str:
    return str(policy["city_tiers"].get(city, policy["default_tier"]))


def trip_days(trip: dict) -> int:
    start = date.fromisoformat(trip["start_date"])
    end = date.fromisoformat(trip["end_date"])
    return (end - start).days + 1


def _time(s: str):
    return datetime.strptime(s, "%H:%M").time()


def check_claim(
    store: DataStore,
    claim_id: str,
    evidence_ids: list[str] | None = None,
    human_decisions: dict[str, str] | None = None,
) -> ClaimCheck:
    claim = store.get_claim(claim_id)
    policy = store.policy
    emp = claim["employee_id"]
    trip = claim["trip"]
    tier = city_tier(policy, trip["destination"])
    cap = _d(policy["hotel_nightly_cap"][tier])
    human_decisions = dict(human_decisions or {})
    for item_id, choice in human_decisions.items():
        if choice not in HUMAN_CHOICES:
            raise ValueError(f"human_decisions[{item_id}] must be one of {sorted(HUMAN_CHOICES)}")

    # Resolve evidence; keep only evidence that belongs to this employee.
    evidence: dict[str, tuple[str, dict]] = {}
    ignored: list[str] = []
    for eid in evidence_ids or []:
        found = store.find_evidence(eid)
        if not found:
            ignored.append(eid)
            continue
        kind, row = found
        owners = row.get("employee_ids") or [row.get("owner_id")]
        if emp not in owners:
            ignored.append(eid)
            continue
        evidence[eid] = (kind, row)

    seen: dict[tuple, str] = {}
    results: list[ItemResult] = []

    for item in claim["items"]:
        cat = item["category"]
        amount = _d(item["amount"])
        res = ItemResult(item["id"], cat, amount, amount, ItemStatus.OK)
        blocked = False  # True -> needs evidence/human, approvable counted as pending
        rejected = False

        # Rule: date within trip
        if not (trip["start_date"] <= item["date"] <= trip["end_date"]):
            res.findings.append(Finding("dates_within_trip", ItemStatus.REJECTED,
                f"Expense date {item['date']} is outside the trip {trip['start_date']}..{trip['end_date']}."))
            rejected = True

        # Rule: duplicates (same category, date, amount)
        key = (cat, item["date"], amount)
        if key in seen:
            res.findings.append(Finding("duplicate", ItemStatus.REJECTED,
                f"Duplicate of {seen[key]} (same category, date and amount)."))
            rejected = True
        else:
            seen[key] = item["id"]

        # Rule: receipt required
        if cat in policy["receipt_required_categories"] and not item.get("receipt"):
            res.findings.append(Finding("receipt_required", ItemStatus.NEEDS_EVIDENCE,
                "Receipt missing.", hint="search_inbox for the receipt or ask the employee"))
            blocked = True

        # Rule: boarding pass for flights
        if cat in policy["boarding_pass_required_for"] and not item.get("boarding_pass"):
            bp = [eid for eid, (k, r) in evidence.items() if k == "inbox" and any(
                a.get("type") == "boarding_pass" and a.get("date") == item["date"] for a in r.get("attachments", []))]
            if bp:
                res.evidence_used += bp
                res.findings.append(Finding("boarding_pass_required", ItemStatus.OK,
                    f"Boarding pass found in {bp[0]}."))
            else:
                res.findings.append(Finding("boarding_pass_required", ItemStatus.NEEDS_EVIDENCE,
                    "Boarding pass missing.", hint=f"search_inbox(employee_id='{emp}', query='boarding pass')"))
                blocked = True

        # Rule: hotel nightly cap by city tier (or signed memo)
        if cat == "hotel":
            nights = int(item.get("nights", 1))
            per_night = amount / nights
            if per_night > cap:
                memo = next((eid for eid, (k, r) in evidence.items() if k == "memo"
                             and r["type"] == "hotel_cap_exception" and r["signed"]
                             and r["city"].lower() == trip["destination"].lower()
                             and r["valid_from"] <= item["date"] <= r["valid_to"]), None)
                if memo:
                    memo_cap = _d(evidence[memo][1]["approved_nightly_cap"])
                    res.evidence_used.append(memo)
                    if per_night <= memo_cap:
                        res.findings.append(Finding("hotel_nightly_cap", ItemStatus.OK,
                            f"{per_night}/night exceeds tier-{tier} cap {cap} but is covered by signed memo {memo} (cap {memo_cap})."))
                    else:
                        res.approvable = memo_cap * nights
                        res.findings.append(Finding("hotel_nightly_cap", ItemStatus.PARTIAL,
                            f"Above memo cap {memo_cap}/night; capped."))
                else:
                    choice = human_decisions.get(item["id"])
                    capped = cap * nights
                    if choice == "approve_full":
                        res.findings.append(Finding("hotel_nightly_cap", ItemStatus.OK,
                            "Over cap, approved in full by a human reviewer."))
                    elif choice == "approve_capped":
                        res.approvable = capped
                        res.findings.append(Finding("hotel_nightly_cap", ItemStatus.PARTIAL,
                            f"Over cap; human reviewer approved the capped amount ({cap}/night x {nights})."))
                    elif choice == "reject":
                        rejected = True
                        res.findings.append(Finding("hotel_nightly_cap", ItemStatus.REJECTED, "Rejected by a human reviewer."))
                    else:
                        res.approvable = capped
                        res.findings.append(Finding("hotel_nightly_cap", ItemStatus.NEEDS_EVIDENCE,
                            f"{per_night}/night exceeds tier-{tier} cap {cap}/night for {trip['destination']}.",
                            hint="find_approval_memo; if none, ask a human: approve_full, approve_capped or reject"))
                        blocked = True
            else:
                res.findings.append(Finding("hotel_nightly_cap", ItemStatus.OK, f"{per_night}/night within cap {cap}."))

        # Rule: per diem
        if cat == "per_diem":
            allowed = _d(policy["per_diem_daily"][tier]) * trip_days(trip)
            if amount > allowed:
                res.approvable = allowed
                res.findings.append(Finding("per_diem", ItemStatus.PARTIAL,
                    f"Per diem allowed {allowed} ({trip_days(trip)} days x tier-{tier} rate)."))
            else:
                res.findings.append(Finding("per_diem", ItemStatus.OK, f"Within allowance {allowed}."))

        # Rule: late check-out justified by a business meeting
        if cat == "late_checkout":
            lc = policy["late_checkout"]
            max_fee = cap * _d(lc["max_fee_ratio_of_nightly_cap"])
            std = _time(lc["standard_checkout_time"])
            meeting = next((eid for eid, (k, r) in evidence.items() if k == "calendar"
                            and r["type"] == "business" and r["date"] == item["date"] and _time(r["end"]) > std), None)
            if meeting:
                res.evidence_used.append(meeting)
                if amount > max_fee:
                    res.approvable = max_fee
                    res.findings.append(Finding("late_checkout", ItemStatus.PARTIAL,
                        f"Justified by {meeting}, but capped at {max_fee}."))
                else:
                    res.findings.append(Finding("late_checkout", ItemStatus.OK,
                        f"Justified by business meeting {meeting} ending after {lc['standard_checkout_time']}."))
            else:
                res.findings.append(Finding("late_checkout", ItemStatus.NEEDS_EVIDENCE,
                    "No business reason found for the late check-out.",
                    hint=f"get_calendar(employee_id='{emp}', date_from='{item['date']}', date_to='{item['date']}')"))
                blocked = True

        # Combine
        if rejected:
            res.status, res.approvable = ItemStatus.REJECTED, Decimal(0)
        elif blocked:
            res.status = ItemStatus.NEEDS_EVIDENCE
            if cat != "hotel":
                res.approvable = Decimal(0)
        elif res.approvable < res.claimed:
            res.status = ItemStatus.PARTIAL
        results.append(res)

    total_claimed = sum((r.claimed for r in results), Decimal(0))
    total_ok = sum((r.approvable for r in results if r.status != ItemStatus.NEEDS_EVIDENCE), Decimal(0))
    pending = sum((r.claimed for r in results if r.status == ItemStatus.NEEDS_EVIDENCE), Decimal(0))
    # For a blocked hotel item, the capped amount is certain; only the excess is pending.
    for r in results:
        if r.status == ItemStatus.NEEDS_EVIDENCE and r.category == "hotel":
            total_ok += r.approvable
            pending -= r.approvable

    statuses = {r.status for r in results}
    if ItemStatus.NEEDS_EVIDENCE in statuses:
        decision = Decision.NEEDS_INFO
    elif statuses == {ItemStatus.OK}:
        decision = Decision.APPROVE
    elif total_ok == 0:
        decision = Decision.REJECT
    else:
        decision = Decision.PARTIAL

    return ClaimCheck(claim_id, emp, total_claimed, total_ok, pending, decision, results, ignored)
