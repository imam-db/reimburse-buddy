"""Simulate the three demo scenarios without WorkBuddy.

Usage: python -m reimburse_buddy.demo {boarding-pass|hotel-memo|hotel-ask|late-checkout|all}

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
                say("  finding", f"{r.item_id} {f.rule}: {f.message}" + (f" -> hint: {f.hint}" if f.hint else ""))


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
    c = check_claim(store, cid, [e["id"] for e in events]); show_check(c)
    print("\n" + draft_decision(store, c) + "\n")


SCENARIOS = {
    "boarding-pass": boarding_pass,
    "hotel-memo": lambda: hotel(False),
    "hotel-ask": lambda: hotel(True),
    "late-checkout": late_checkout,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    name = argv[0] if argv else "all"
    if name == "all":
        for n, fn in SCENARIOS.items():
            print(f"===== {n} =====")
            fn()
        return 0
    if name not in SCENARIOS:
        print(__doc__)
        return 2
    SCENARIOS[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
