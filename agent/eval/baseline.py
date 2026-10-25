"""No-LLM baseline: the same graph, engine, gates and scoring, with a fixed heuristic policy in place of the
model. python -m eval.run_hard_eval --model baseline (no key, $0).

The policy only reads structured tool output (JSON) and never reads free text for meaning:
  1. get_claim + check_claim with no evidence;
  2. per open gap, up to two mailbox searches built from the item's fields (flight no, description words,
     category words, date) plus fixed keywords (receipt/struk/kuitansi, boarding pass, folio, and for a late
     check-out moved/reschedul/diundur), plus get_calendar for the trip (late check-out) and
     find_approval_memo for the trip city (hotel over cap);
  3. check_claim with every ID those returned (the engine keeps what matches);
  4. request_missing_evidence (fixed template) for each pure document gap still open, then one
     "<claim> <item>" search for the reply and a re-check;
  5. a gap that needs a reviewer: a fixed question; the reviewer answers through a structured form
     (reviewer_decisions, no language parsing);
  6. draft_decision with the evidence the engine used.
Batch cases use the batch graph unchanged; each sub-run runs this same policy."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

CLAIM = re.compile(r"\bCLM-\d{4}-\d{4}\b")
IDS = re.compile(r'"id"\s*:\s*"((?:MSG|EVT|MEMO)-[\w-]+)"')
FREE = ("receipt_missing", "boarding_pass_missing", "hotel_nights_mismatch")
SEARCH_TOOLS = ("search_for_gap", "search_reimburse_mailbox", "get_calendar", "find_approval_memo")
CAT_WORDS = {"taxi": "taxi taksi", "flight": "flight", "hotel": "hotel", "meal": "meal",
             "late_checkout": "late checkout hotel"}
GAP_WORDS = {"receipt_missing": "receipt struk kuitansi", "boarding_pass_missing": "boarding pass",
             "hotel_nights_mismatch": "folio nights", "late_checkout_unjustified": "moved reschedul diundur",
             "hotel_over_cap": "memo approval", "duplicate_suspected": "receipt"}


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def open_gaps(check: dict) -> list[tuple[str, str]]:
    return [(i["item_id"], f["gap"]) for i in check.get("items", []) for f in i.get("findings", [])
            if f.get("gap") and not f.get("resolved_by")]


class BaselineModel(BaseChatModel):
    """Deterministic, history-driven policy. Reports zero token usage."""

    _tool_names: set = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        return "no-llm-baseline"

    def bind_tools(self, tools, **kw):
        self._tool_names = {t.name for t in tools}
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        msg = self.decide(messages)
        msg.usage_metadata = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        return ChatResult(generations=[ChatGeneration(message=msg)])

    # --- policy ---------------------------------------------------------------------------------------
    def decide(self, messages) -> AIMessage:
        humans = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
        first = str(messages[humans[0]].content) if humans else ""
        m = CLAIM.search(first)
        if not m:
            return AIMessage(content="No claim ID in the request; the baseline handles one claim per run.")
        cid = m.group(0)
        calls, results = [], {}
        for i, msg in enumerate(messages):
            for c in getattr(msg, "tool_calls", None) or []:
                calls.append((i, c["name"], c.get("args") or {}, c["id"]))
            if getattr(msg, "type", "") == "tool":
                results[msg.tool_call_id] = str(msg.content)
        n = len([x for x in messages if isinstance(x, AIMessage)])

        def mk(*pairs):
            return AIMessage(content="", tool_calls=[{"name": nm, "args": a, "id": f"bl{n}-{k}"}
                                                     for k, (nm, a) in enumerate(pairs)])

        claim = next((_json(results.get(c[3], "")) for c in calls if c[1] == "get_claim"), None)
        if claim is None:
            return mk(("get_claim", {"claim_id": cid}), ("check_claim", {"claim_id": cid}))
        checks = [(c, _json(results.get(c[3], "")) or {}) for c in calls if c[1] == "check_claim"]
        last_call, last = checks[-1] if checks else (None, {})
        if not last or "error" in last or "items" not in last:  # a failed check: try again once more
            ev = (last_call[2].get("evidence_ids") if last_call else None) or []
            if len([1 for _c, d in checks if "items" not in d]) <= 2:
                return mk(("check_claim", {"claim_id": cid, **({"evidence_ids": ev} if ev else {})}))
        passed = list((last_call[2].get("evidence_ids") if last_call else None) or [])
        ignored = {r.get("id") for r in last.get("ignored_evidence", []) or [] if isinstance(r, dict)}
        used = [e for e in passed if e not in ignored]
        gaps = open_gaps(last)
        last_human = humans[-1] if humans else 0
        after_human = [c for c in calls if c[0] > last_human]
        emp = claim.get("employee_id") or last.get("employee_id")
        trip = claim.get("trip") or {}
        items = {i["id"]: i for i in claim.get("items", [])}

        # A reviewer turn (form decisions are injected by the graph): re-check, then draft.
        if len(humans) > 1 and not after_human:
            return mk(("check_claim", {"claim_id": cid, **({"evidence_ids": used} if used else {})}))

        searched = [c for c in calls if c[1] in SEARCH_TOOLS]
        if gaps and not searched:
            out = []
            for item_id, gap in gaps:
                it = items.get(item_id, {})
                words = " ".join(x for x in (it.get("flight_no"), it.get("description"),
                                              CAT_WORDS.get(it.get("category", ""), ""), GAP_WORDS.get(gap, ""),
                                              it.get("date")) if x)
                out.append(("search_for_gap", {"item_id": item_id, "gap_code": gap, "query": words}))
                if gap == "late_checkout_unjustified":
                    out.append(("get_calendar", {"employee_id": emp, "date_from": trip.get("start_date", it.get("date")),
                                                 "date_to": trip.get("end_date", it.get("date"))}))
                if gap == "hotel_over_cap":
                    out.append(("find_approval_memo", {"employee_id": emp, "city": trip.get("destination")}))
            return mk(*out)

        def found(cs):
            ids = []
            for c in cs:
                for x in IDS.findall(results.get(c[3], "")):
                    if x not in ids:
                        ids.append(x)
            return ids

        last_search = max((c[0] for c in searched), default=-1)
        last_check = max((c[0] for c in calls if c[1] == "check_claim"), default=-1)
        if searched and last_check < last_search:
            cand = [e for e in found(searched) if e not in passed]
            if cand:
                return mk(("check_claim", {"claim_id": cid, "evidence_ids": [*used, *cand]}))

        asked = {(c[2].get("item_id"), c[2].get("gap_code")) for c in calls if c[1] == "request_missing_evidence"}
        to_ask = [(i, g) for i, g in gaps if g in FREE and (i, g) not in asked]
        if to_ask and "request_missing_evidence" in self._tool_names and len(humans) == 1:
            return mk(*[("request_missing_evidence", {"claim_id": cid, "item_id": i, "gap_code": g,
                                                      **({"evidence_ids": used} if used else {})}) for i, g in to_ask])

        last_ask = max((c[0] for c in calls if c[1] == "request_missing_evidence"), default=-1)
        if last_ask >= 0 and not any(c[1] == "search_for_gap" and c[0] > last_ask for c in calls):
            open_asked = [(i, g) for i, g in gaps if (i, g) in asked]
            if open_asked:
                return mk(*[("search_for_gap", {"item_id": i, "gap_code": g, "query": f"{cid} {i}"})
                            for i, g in open_asked])

        need_reviewer = [(i, g) for i, g in gaps if g not in FREE]
        if need_reviewer and len(humans) == 1:
            listed = ", ".join(f"{i} {g}" for i, g in need_reviewer)
            return AIMessage(content=f"Reviewer decision needed (use the decision form) for: {listed}?")

        if not any(c[1] == "draft_decision" for c in after_human):
            return mk(("draft_decision", {"claim_id": cid, **({"evidence_ids": used} if used else {})}))
        return AIMessage(content="Open gaps: " + (", ".join(f"{i} {g}" for i, g in gaps) or "none") + ".")
