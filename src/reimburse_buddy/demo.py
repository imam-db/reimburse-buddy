"""Simulate the three demo scenarios without WorkBuddy.

Usage: python -m reimburse_buddy.demo {triage|hero|boarding-pass|hotel-memo|hotel-ask|late-checkout|all}

Each scenario scripts what the agent would do: plan, call tools, revise the plan, decide.
"""

from __future__ import annotations

import json
import sys

from .data_store import DataStore
from .decision import draft_decision
from .models import rupiah
from .rules import check_claim

store = DataStore()


def say(tag: str, text: str) -> None:
    print(f"[{tag}] {text}")


def show_check(c) -> None:
    say("check_claim", f"decision={c.decision.value} approved={rupiah(c.total_approvable)} pending={rupiah(c.total_pending)}")
    for r in c.items:
        for f in r.findings:
            if f.status.value != "ok":
                say("  finding", f"{r.item_id} {f.rule}: {f.message}" + (f" -> gap: {f.gap}" if f.gap else ""))


def boarding_pass() -> None:
    cid, emp = "CLM-2026-0101", "EMP-001"
    say("plan", "1) get_claim 2) check_claim 3) resolve gaps 4) draft_decision")
    c = check_claim(store, cid); show_check(c)
    say("plan revised", "Boarding pass missing -> insert step: search_inbox for 'boarding pass'")
    hits = store.search_inbox(emp, "boarding pass")
    say("search_inbox", json.dumps([h["id"] + ": " + h["subject"] for h in hits]))
    ev = [h["id"] for h in hits]
    c = check_claim(store, cid, ev); show_check(c)
    print("\n" + draft_decision(store, c) + "\n")


def hotel(ask_human: bool) -> None:
    if not ask_human:
        cid, emp, city, d = "CLM-2026-0102", "EMP-002", "Jakarta", "2026-10-14"
    else:
        cid, emp, city, d = "CLM-2026-0103", "EMP-002", "Medan", "2026-10-20"
    say("plan", "1) get_claim 2) check_claim 3) resolve gaps 4) draft_decision")
    c = check_claim(store, cid); show_check(c)
    say("plan revised", "Hotel over cap -> insert step: find_approval_memo")
    memos = [m for m in store.find_approval_memo(emp, city, d) if m["signed"]]
    say("find_approval_memo", json.dumps([m["id"] for m in memos]))
    if memos:
        c = check_claim(store, cid, [m["id"] for m in memos]); show_check(c)
        print("\n" + draft_decision(store, c) + "\n")
        return
    say("ask human", "No signed memo. Approve in full, approve the capped amount, or reject the hotel item?")
    say("human", "approve_capped")
    c = check_claim(store, cid, [], {"IT-1": "approve_capped"}); show_check(c)
    print("\n" + draft_decision(store, c, note="Capped per reviewer; no exception memo.") + "\n")


def late_checkout() -> None:
    cid, emp = "CLM-2026-0104", "EMP-003"
    say("plan", "1) get_claim 2) check_claim 3) resolve gaps 4) draft_decision")
    c = check_claim(store, cid); show_check(c)
    say("plan revised", "Late check-out unexplained -> insert step: get_calendar for 2026-10-21")
    events = store.get_calendar(emp, "2026-10-21", "2026-10-21")
    say("get_calendar", json.dumps([f"{e['id']} {e['start']}-{e['end']} {e['title']}" for e in events]))
    ev = [e["id"] for e in events]
    c = check_claim(store, cid, ev); show_check(c)
    say("plan revised", "IT-6 duplicate_suspected -> insert step: ask Sari for the second taxi receipt")
    sent = store.send_message(emp, "Hi Sari, two taxis on 20 Oct for Rp120.000 each (IT-6). Was it a separate "
                              "ride? Please send the second receipt.", "Claim CLM-2026-0104: taxi IT-6", cid)
    say("send_message", json.dumps(sent))
    hits = store.search_inbox(emp, "taxi receipt")
    say("search_inbox", json.dumps([h["id"] + ": " + h["subject"] for h in hits]))
    ev += [h["id"] for h in hits]
    c = check_claim(store, cid, ev); show_check(c)
    say("update_claim_status", json.dumps(store.update_claim_status(cid, "partially_approved", "IT-7 outside trip")))
    print("\n" + draft_decision(store, c) + "\n")


def hero() -> None:
    """One claim, four problems: dead-end search, unsigned memo trap, cross-source meeting, missing receipt."""
    cid, emp = "CLM-2026-0110", "EMP-001"
    say("plan", "1) get_claim 2) check_claim 3) resolve each gap 4) ask the reviewer once 5) draft_decision 6) follow-up actions")
    c = check_claim(store, cid); show_check(c)
    ev: list[str] = []
    say("plan revised", "4 gaps -> 3a boarding pass, 3b hotel memo, 3c late check-out, 3d taxi receipt")
    # 3a: dead end, then an alternative query
    hits = store.search_inbox(emp, "boarding pass")
    say("search_inbox 'boarding pass'", json.dumps([h["id"] + ": " + h["subject"] for h in hits]))
    probe = check_claim(store, cid, [hits[0]["id"]])
    say("check_claim", f"top hit {hits[0]['id']} -> " + ", ".join(f"{i.id}: {i.reason}" for i in probe.ignored_evidence))
    hits = store.search_inbox(emp, "QZ 7520")
    say("search_inbox 'QZ 7520'", json.dumps([h["id"] + ": " + h["subject"] for h in hits]))
    ev += [h["id"] for h in hits]
    # 3b: memo found, but unsigned
    memos = store.find_approval_memo(emp, "Surabaya", "2026-10-02")
    say("find_approval_memo", json.dumps([(m["id"], "signed" if m["signed"] else "UNSIGNED") for m in memos]))
    ev += [m["id"] for m in memos]
    # 3c: calendar says 11:30, an email says the meeting moved
    cal = store.get_calendar(emp, "2026-10-04", "2026-10-04")
    say("get_calendar", json.dumps([f"{e['id']} {e['start']}-{e['end']}" for e in cal]))
    ev += [e["id"] for e in cal]
    c = check_claim(store, cid, ev); show_check(c)
    say("plan revised", "Calendar meeting ends 11:30, before check-out time -> check email for a reschedule")
    hits = store.search_inbox(emp, "meeting moved")
    say("search_inbox 'meeting moved'", json.dumps([h["id"] + ": " + h["subject"] for h in hits]))
    ev += [h["id"] for h in hits]
    # 3d: ask the employee for the receipt
    say("send_message", json.dumps(store.send_message(emp, "Hi Rina, please send the taxi receipt for 3 Oct (IT-5).",
                                                      "Claim CLM-2026-0110: taxi receipt", cid)))
    ev += [h["id"] for h in store.search_inbox(emp, "taxi receipt")]
    c = check_claim(store, cid, ev); show_check(c)
    for i in c.ignored_evidence:
        say("ignored", f"{i.id}: {i.reason}")
    say("ask human", "IT-3 hotel is Rp1.350.000/night vs Rp1.200.000 cap; MEMO-2026-038 is unsigned. "
                     "approve_full, approve_capped or reject?")
    say("human", "approve_capped")
    c = check_claim(store, cid, ev, {"IT-3": "approve_capped"}); show_check(c)
    say("update_claim_status", json.dumps(store.update_claim_status(cid, "partially_approved", "Hotel capped; memo unsigned")))
    print("\n" + draft_decision(store, c) + "\n")


def triage() -> None:
    from .triage import triage_claims
    t = triage_claims(store, "2026-W43")
    say("triage_claims", f"{t['claims']} claims: {len(t['auto_cleared'])} auto-cleared, {len(t['exceptions'])} exceptions")
    for e in t["exceptions"]:
        say("  exception", f"{e['claim_id']} gaps={e['gaps']} rejected={e['rejected_items']}")
    say("time saved", f"~{t['time_saved_estimate']['minutes']} min ({t['time_saved_estimate']['assumption']})")


SCENARIOS = {
    "triage": triage,
    "hero": hero,
    "boarding-pass": boarding_pass,
    "hotel-memo": lambda: hotel(False),
    "hotel-ask": lambda: hotel(True),
    "late-checkout": late_checkout,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    name = argv[0] if argv else "all"
    if name == "all":
        global store
        for n, fn in SCENARIOS.items():
            print(f"===== {n} =====")
            store = DataStore()  # fresh mock state per scenario
            fn()
        return 0
    if name not in SCENARIOS:
        print(__doc__)
        return 2
    SCENARIOS[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
