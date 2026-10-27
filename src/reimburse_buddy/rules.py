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
from .messages import render
from .models import ClaimCheck, Decision, Finding, IgnoredEvidence, ItemResult, ItemStatus

HUMAN_CHOICES = {"approve_full", "approve_capped", "reject"}
USE_RECEIPT = "use:"  # human_decisions value "use:MSG-xxxx" names the receipt to pay when several disagree
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


def F(rule: str, status: ItemStatus, key: str, gap: str | None = None, **params) -> Finding:
    params = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in params.items()}
    return Finding(rule, status, render(key, params), gap, key, params)


SHAPE_HELP = ('Expected evidence_ids as a list of strings, e.g. ["MSG-1234", "EVT-5678"], and '
              'human_decisions as an object mapping item IDs to a choice, e.g. {"IT-3": "approve_capped"}. '
              'Choices: approve_full, approve_capped, reject.')


def _content_key(eid: str, a: dict) -> tuple:
    """Same receipt = same receipt number, else same file hash, else same message and file name."""
    if a.get("receipt_no"):
        return ("no", a["receipt_no"])
    if a.get("sha256"):
        return ("sha", a["sha256"])
    return ("att", eid, a.get("name"))


def _dedupe(pairs: list[tuple]) -> list[tuple]:
    out, seen = [], set()
    for e, a in pairs:
        k = _content_key(e, a)
        if k not in seen:
            seen.add(k)
            out.append((e, a))
    return out


def _validate_shapes(evidence_ids, human_decisions) -> None:
    if evidence_ids is not None and (not isinstance(evidence_ids, list)
                                     or not all(isinstance(e, str) for e in evidence_ids)):
        raise ValueError(f"evidence_ids has the wrong shape ({type(evidence_ids).__name__}). {SHAPE_HELP}")
    if human_decisions is not None and (not isinstance(human_decisions, dict)
                                        or not all(isinstance(k, str) and isinstance(v, str)
                                                   for k, v in human_decisions.items())):
        raise ValueError(f"human_decisions has the wrong shape ({type(human_decisions).__name__}). {SHAPE_HELP}")
    for item_id, choice in (human_decisions or {}).items():
        if choice not in HUMAN_CHOICES and not (choice.startswith(USE_RECEIPT) and len(choice) > len(USE_RECEIPT)):
            raise ValueError(f"human_decisions[{item_id!r}] = {choice!r} is not a valid choice. {SHAPE_HELP}")


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
    updates = []
    for mid, (k, row) in evidence.items():
        upd = row.get("meeting_update") if k == "inbox" else None
        if upd and upd["event_id"] in events:
            updates.append((str(row.get("date") or ""), mid, upd))
    # Apply oldest first so the latest-dated message wins; message id breaks date ties.
    updates.sort(key=lambda item: (item[0], item[1]))
    for _, mid, upd in updates:
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
    _validate_shapes(evidence_ids, human_decisions)
    human_decisions = dict(human_decisions or {})
    unknown_items = set(human_decisions) - {i["id"] for i in claim["items"]}
    if unknown_items:
        raise ValueError(f"human_decisions refers to unknown item(s) {sorted(unknown_items)} in {claim_id}. {SHAPE_HELP}")

    evidence, ignored = _resolve_evidence(store, emp, evidence_ids)
    all_receipts: dict[tuple, list] = {}
    for eid, a in _attachments(evidence):
        if a.get("type") == "receipt":
            all_receipts.setdefault((a.get("claim_id"), a.get("item_id")), []).append((eid, a))
    receipts = {k: v[0] for k, v in all_receipts.items()}
    amount_required = bool(policy.get("receipt_amount_required"))
    # Every receipt in the employee's mailbox for this claim, whatever evidence was passed.
    mailbox: dict[tuple, list] = {(claim_id, i["id"]): [] for i in claim["items"]}
    receipt_items: dict[tuple, set] = {}
    for m in store.inbox:
        if m.get("owner_id") != emp:
            continue
        for a in m.get("attachments", []):
            if a.get("type") == "receipt" and a.get("claim_id") == claim_id and (claim_id, a.get("item_id")) in mailbox:
                mailbox[(claim_id, a["item_id"])].append((m["id"], a))
                if not a.get("receipt_no"):  # numbered receipts reused across items are caught as duplicates below
                    receipt_items.setdefault(_content_key(m["id"], a), set()).add(a["item_id"])

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

        def gap(rule, code, key, **params):
            res.findings.append(F(rule, NEEDS, key, code, **params))
            res.gaps.append(code)

        # Category must be on the policy's allowed list
        if cat not in policy.get("allowed_categories", [cat]):
            res.findings.append(F("category_allowed", ItemStatus.REJECTED, "category_not_allowed",
                                  category=cat, allowed=", ".join(policy["allowed_categories"])))
            rejected = True

        # Dates within trip
        if not (trip["start_date"] <= item["date"] <= trip["end_date"]):
            res.findings.append(F("dates_within_trip", ItemStatus.REJECTED, "date_outside",
                                  date=item["date"], start=trip["start_date"], end=trip["end_date"]))
            rejected = True

        # Receipt required (a receipt found in the inbox counts)
        receipt_cap = None  # what a receipt supports; approve_capped / use:MSG pay at most this
        options: dict[str, int | None] = {}  # receipt choices for multiple_receipts, by message ID
        if cat in policy["receipt_required_categories"] and not item.get("receipt"):
            found = receipts.get((claim_id, iid))
            if not found:
                gap("receipt_required", G.RECEIPT_MISSING, "receipt_missing")
            else:
                res.evidence_used += [e for e, _ in all_receipts[(claim_id, iid)] if e not in res.evidence_used]
                # Compare against every receipt the mailbox holds for this item, not only the ones passed in.
                uniq = _dedupe(all_receipts[(claim_id, iid)] + mailbox[(claim_id, iid)])
                need_amount = amount_required or any(a.get("amount_required") for _, a in uniq)
                bad = [(e, a) for e, a in uniq if a.get("amount_error")]
                missing = [e for e, a in uniq if a.get("amount") is None and not a.get("amount_error")]
                amounts = sorted({a["amount"] for _, a in uniq if a.get("amount") is not None})
                shared = [e for e, a in uniq if len(receipt_items.get(_content_key(e, a), ())) > 1]
                for e, a in bad:
                    gap("receipt_required", G.RECEIPT_AMOUNT_MISMATCH, "receipt_amount_unreadable", src=e, why=a["amount_error"])
                if need_amount and missing:
                    gap("receipt_required", G.RECEIPT_AMOUNT_MISMATCH, "receipt_amount_missing", srcs=", ".join(missing))
                if shared:
                    others = sorted(set().union(*(receipt_items[_content_key(e, a)] for e, a in uniq if e in shared)) - {iid})
                    gap("receipt_required", G.RECEIPT_SHARED, "receipt_shared", srcs=", ".join(shared), others=", ".join(others))
                if len(amounts) > 1:
                    # Receipts that disagree: never pick one silently; the reviewer names one (use:MSG-xxxx).
                    options = {e: a.get("amount") for e, a in uniq}
                    gap("receipt_required", G.MULTIPLE_RECEIPTS, "multiple_receipts", srcs=", ".join(e for e, _ in uniq))
                elif amounts:
                    r = _d(amounts[0])
                    src = next(e for e, a in uniq if a.get("amount") == amounts[0])
                    receipt_cap = r
                    if amount > r:  # claiming more than the receipt shows
                        gap("receipt_required", G.RECEIPT_AMOUNT_MISMATCH, "receipt_amount_mismatch", src=src,
                            receipt_amount=r, item_amount=amount)
                    elif amount < r:  # claiming less than paid is fine, but note it
                        res.findings.append(F("receipt_required", ItemStatus.OK, "receipt_higher", src=src,
                                              receipt_amount=r, item_amount=amount))
                if not res.gaps or all(g == G.DUPLICATE_SUSPECTED for g in res.gaps):
                    if not any(f.key == "receipt_higher" for f in res.findings):
                        res.findings.append(F("receipt_required", ItemStatus.OK, "receipt_found", src=found[0]))

        # Duplicates: same receipt number is a hard duplicate; same category/date/amount
        # without distinct receipt numbers is only suspected.
        rno = receipt_no[iid]
        if rno and not item.get("receipt_no"):
            src = receipts[(claim_id, iid)][0]
            if src not in res.evidence_used:
                res.evidence_used.append(src)
        key = (cat, item["date"], amount)
        if rno and rno in seen_receipts:
            res.findings.append(F("duplicate", ItemStatus.REJECTED, "dup_same_receipt", rno=rno, other=seen_receipts[rno]))
            rejected = True
        elif key in seen_keys:
            other = seen_keys[key]
            if not (rno and receipt_no[other]):
                gap("duplicate", G.DUPLICATE_SUSPECTED, "dup_suspected", other=other)
        if rno:
            seen_receipts.setdefault(rno, iid)
        seen_keys.setdefault(key, iid)

        # Flights: boarding pass must match date, flight number and passenger
        if cat in policy["boarding_pass_required_for"] and not item.get("boarding_pass"):
            match = next((eid for eid, a in _attachments(evidence) if a.get("type") == "boarding_pass"
                          and a.get("date") == item["date"]
                          and _norm(item.get("flight_no"))  # an empty flight number never matches
                          and _norm(a.get("flight")) == _norm(item.get("flight_no"))
                          and _norm(a.get("passenger")) == _norm(emp_name)), None)
            if match:
                res.evidence_used.append(match)
                res.findings.append(F("boarding_pass_required", ItemStatus.OK, "bp_found",
                                      flight=item.get("flight_no"), date=item["date"], name=emp_name, src=match))
            else:
                gap("boarding_pass_required", G.BOARDING_PASS_MISSING, "bp_missing",
                    flight=item.get("flight_no"), date=item["date"], name=emp_name)

        # Hotel: nights inside the trip, nightly cap by tier or signed memo
        if cat == "hotel":
            nights = int(item.get("nights", 1))
            checkout = date.fromisoformat(item["date"]) + timedelta(days=nights)
            if nights < 1 or checkout > date.fromisoformat(trip["end_date"]):
                gap("hotel_nights_within_trip", G.HOTEL_NIGHTS_MISMATCH, "nights_mismatch",
                    nights=nights, date=item["date"], checkout=str(checkout), end=trip["end_date"])
            per_night = amount / nights
            if per_night <= cap:
                res.findings.append(F("hotel_nightly_cap", ItemStatus.OK, "hotel_within", per_night=per_night, cap=cap))
            else:
                memo = next((eid for eid, (k, r) in evidence.items() if k == "memo"
                             and r["type"] == "hotel_cap_exception"
                             and r["city"].lower() == trip["destination"].lower()
                             and r["valid_from"] <= item["date"] <= r["valid_to"]), None)
                if memo:
                    memo_cap = _d(evidence[memo][1]["approved_nightly_cap"])
                    res.evidence_used.append(memo)
                    allowed = min(amount, memo_cap * nights)
                    res.findings.append(F("hotel_nightly_cap",
                        ItemStatus.OK if allowed == amount else ItemStatus.PARTIAL, "hotel_memo",
                        per_night=per_night, tier=tier, cap=cap, memo=memo, memo_cap=memo_cap))
                else:
                    allowed = cap * nights
                    gap("hotel_nightly_cap", G.HOTEL_OVER_CAP, "hotel_over",
                        per_night=per_night, tier=tier, cap=cap, city=trip["destination"])

        # Per diem
        if cat == "per_diem":
            limit = _d(policy["per_diem_daily"][tier]) * trip_days(trip)
            allowed = min(amount, limit)
            res.findings.append(F("per_diem", ItemStatus.OK if allowed == amount else ItemStatus.PARTIAL, "per_diem",
                                  limit=limit, days=trip_days(trip), tier=tier))

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
                res.findings.append(F("late_checkout", ItemStatus.OK if allowed == amount else ItemStatus.PARTIAL,
                                      "lc_ok", time=item.get("checkout_time"), ids=", ".join(ok), location=location,
                                      calendar_only=all(eid.startswith("EVT-") for eid in ok)))
            else:
                gap("late_checkout", G.LATE_CHECKOUT_UNJUSTIFIED, "lc_missing", location=location, date=item["date"],
                    std=lc["standard_checkout_time"], time=item.get("checkout_time"))

        # Combine
        choice = human_decisions.get(iid)
        if rejected or choice == "reject":
            res.status, res.approvable = ItemStatus.REJECTED, Decimal(0)
            if choice == "reject" and not rejected:
                res.findings.append(F("human_decision", ItemStatus.REJECTED, "human_rejected"))
        elif res.gaps and choice and choice.startswith(USE_RECEIPT):
            picked = choice[len(USE_RECEIPT):]
            if picked not in options:
                raise ValueError(f"human_decisions[{iid!r}] = {choice!r}: use:<message ID> only settles "
                                 f"multiple_receipts and must name one of {sorted(options) or 'no receipts'}.")
            res.human_decision = choice
            pick = options[picked]
            res.approvable = min(allowed, _d(pick)) if pick is not None else allowed
            res.status = ItemStatus.OK if res.approvable == amount else ItemStatus.PARTIAL
            for f in res.findings:
                if f.status == NEEDS:
                    f.resolved_by = choice
            res.findings.append(F("human_decision", res.status, "human_resolved", choice=choice))
        elif res.gaps and choice in ("approve_full", "approve_capped"):
            if G.MULTIPLE_RECEIPTS in res.gaps:
                raise ValueError(f"human_decisions[{iid!r}] = {choice!r}: {iid} has receipts that disagree; "
                                 f"name the one to pay with use:<message ID>, one of {sorted(options)}.")
            if receipt_cap is not None:
                allowed = min(allowed, receipt_cap)
            res.human_decision = choice
            res.approvable = amount if choice == "approve_full" else allowed
            res.status = ItemStatus.OK if res.approvable == amount else ItemStatus.PARTIAL
            for f in res.findings:
                if f.status == NEEDS:
                    f.resolved_by = choice
            res.findings.append(F("human_decision", res.status, "human_resolved", choice=choice))
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
        warnings.append(F("return_flight", NEEDS, "return_missing", G.RETURN_FLIGHT_MISSING))

    # Evidence that was accepted but supported nothing is reported, not silently dropped.
    used = {e for r in results for e in r.evidence_used}
    ignored += [IgnoredEvidence(eid, "no_matching_item") for eid in evidence if eid not in used]

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
