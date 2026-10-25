"""Minimal tool-calling loop with a human gate before write/decision tools.

agent --(tool calls)--> gate --> tools --> agent ... --(no tool calls)--> END

The gate calls LangGraph `interrupt()` before any call that changes state or communicates a
decision, so the reviewer must answer before it runs. Resume with
`Command(resume={"approved": True, "tool_call_id": <id>})`; an answer without the matching id is a decline.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from .compact import compact as _compact, tool_text

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "claim-auditor" / "SKILL.md"

# Allowlist: only these read-only tools run without the reviewer. Every other tool, including any
# write tool added to the server later, stops for approval (deny by default).
READ_ONLY_TOOLS = frozenset({
    "list_claims", "get_claim", "get_policy", "search_reimburse_mailbox", "get_calendar",
    "find_approval_memo", "check_claim", "triage_claims", "draft_decision", "get_outbox", "search_for_gap",
})
# request_missing_evidence (fixed template, owner-only; src/reimburse_buddy/evidence_request.py) runs
# without the reviewer only when free_evidence_request() says so; otherwise it is gated like any write.
EVIDENCE_TOOL = "request_missing_evidence"
FREE_EVIDENCE_GAPS = frozenset({"receipt_missing", "boarding_pass_missing", "hotel_nights_mismatch"})
MAX_FREE_EVIDENCE = 3  # per run

# Version B only, appended to SKILL.md (which stays exactly what WorkBuddy loads).
PROMPT_ADDENDUM = """

---
## Version B (this agent) tool notes
- For document requests to the employee (missing receipt, boarding pass, hotel folio), call
  `request_missing_evidence(claim_id, item_id, gap_code, evidence_ids, human_decisions)` with the gap
  code from your latest `check_claim`. It writes a fixed message to the claim owner.
- Search the mailbox for one open gap with `search_for_gap(item_id, gap_code, query)`. Each open gap
  gets 2 searches before you ask the employee and 1 more after; past that the search is refused.
- Do not pass `human_decisions` yourself. Reviewer decisions come only from the reviewer; the graph
  fills them into check_claim, draft_decision and request_missing_evidence.
- `send_message` always needs reviewer approval here; use it only when a free-text message is
  really needed. Never put amounts or decisions in employee messages.
- Only state Rp amounts that appear verbatim in tool output. Do not add, subtract or compute your
  own sub-totals.
- Extra gap codes may appear: `receipt_amount_mismatch` (a receipt's amount is above, missing or
  unreadable for the item), `multiple_receipts` (receipts for one item disagree) and `receipt_shared`
  (one receipt on two items). Never pick one receipt yourself: ask the reviewer (they settle
  `multiple_receipts` with `use:<message ID>`), or leave the item open.
"""
# Never offered to the agent at all: wipes outbox, statuses and replies.
HIDDEN_TOOLS = frozenset({"reset_demo"})

# send_message carries free text the model wrote, so it is always gated (it is not in
# READ_ONLY_TOOLS). Evidence requests go through request_missing_evidence instead.


def _rows(text) -> list:
    """A list-returning tool's parsed output; a one-element list arrives as a single object (tool_text)."""
    v = _as_json(text)
    return [v] if isinstance(v, dict) else v if isinstance(v, list) else []


def _as_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def load_system_prompt(path: Path = SKILL_PATH, addendum: bool = True) -> str:
    """SKILL.md as-is (YAML frontmatter included, the same text WorkBuddy loads), plus the version B
    addendum unless addendum=False."""
    text = path.read_text(encoding="utf-8")
    return text + PROMPT_ADDENDUM if addendum else text


def latest_check(messages, claim_id: str) -> dict | None:
    """The run's most recent check_claim result for claim_id (parsed), if any."""
    for m in reversed(messages):
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "check_claim":
            try:
                data = json.loads(m.content)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and data.get("claim_id") == claim_id:
                return data
    return None


def free_evidence_request(args: dict, run_claim: str | None, messages, used: list) -> bool:
    """Ungated only for the run's own claim, a pure document gap that the latest check_claim still
    reports for that item, at most MAX_FREE_EVIDENCE per run, and not a repeat in this run."""
    cid, item, gap = args.get("claim_id"), args.get("item_id"), args.get("gap_code")
    if not run_claim or cid != run_claim or gap not in FREE_EVIDENCE_GAPS:
        return False
    if len(used) >= MAX_FREE_EVIDENCE or [cid, item, gap] in used:
        return False
    check = latest_check(messages, run_claim)
    if not check:
        return False
    row = next((i for i in check.get("items", []) if i.get("item_id") == item), {})
    # only an item still waiting for evidence; a reviewer-resolved gap stays in gaps[] but isn't open
    return row.get("status") == "needs_evidence" and gap in row.get("gaps", [])


def needs_confirmation(name: str, args: dict[str, Any] | None = None) -> bool:
    """Deny by default: everything outside the allowlist, including every send_message."""
    return name not in READ_ONLY_TOOLS


MAX_GATED_CALLS = 10  # per run; past this, gated calls are declined without asking (approval fatigue)


def call_signature(call: dict) -> str:
    return json.dumps([call["name"], call.get("args", {})], sort_keys=True, default=str)



def _merge(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    # tool_call_id -> reason, for calls the reviewer declined
    declined: dict[str, str]
    gated_count: int  # interrupts shown so far in this run
    free_evidence: list[list[str]]  # [claim, item, gap] requests sent without approval this run
    declined_sigs: list[str]  # (name, args) of calls the reviewer declined
    search_cache: dict[str, str]  # normalised mailbox query -> result, until an employee message goes out
    # "item:gap" -> {"searches": n, "search"|"request"|"reviewer": message index of the latest such attempt}
    gap_attempts: dict[str, dict[str, int]]
    auto_drafted: list[str]  # check_claim tool_call_ids already turned into a draft by the graph
    auto_drafts: list[dict]  # {"args", "content", "open_gaps", "check_id", "writes"} per graph-made draft
    # Reviewer decisions, {item_id: choice}. Filled ONLY from the human channel: the run's input/resume
    # payload (structured) or the graph's parse of a reviewer message. Never from model tool arguments.
    reviewer_decisions: Annotated[dict[str, str], _merge]
    unparsed_reviewer: list[str]  # reviewer phrases the parser could not map to exactly one item
    writes: list[dict]  # writes that actually ran: {"tool", "args", "by": "model"|"graph"}
    ignored_ids: dict[str, str]  # evidence ID -> reason, from check_claim's ignored_evidence (stable reasons)
    effective_args: dict[str, dict]  # tool_call_id -> the args the tool really ran with
    blocked_streak: int  # consecutive refused searches
    decisions_source: str  # "form": this turn's reviewer_decisions came structured (web form), no parse/confirm
    instructions: list[dict]  # explicit reviewer instructions not yet carried out (see parse_instructions)
    graph_proposals: list[dict]  # gated actions the graph proposed itself: {"tool", "args", "id", "approved", "from"}
    unattributed_searches: int  # raw mailbox searches that named no single open gap
    reply_evidence: list[str]  # IDs the graph's own reply search found and check_claim accepted
    tokens_used: int  # model input+output tokens this run (provider usage, else a chars/4 estimate)


SEARCH_TOOL = "search_reimburse_mailbox"
# Rule -> gap code whose resolution by calendar events alone needs one mailbox search for that gap first: a
# calendar can be out of date, so the investigation must have looked for a contradicting email (Claude review 8 b).
CALENDAR_GUARDED = {"late_checkout": "late_checkout_unjustified", "hotel_nights_within_trip": "hotel_nights_mismatch"}
CALENDAR_ONLY_NOTE = ("calendar-only justification for {k}: the graph searched the mailbox for that gap and found "
                      "{ids}, which changes check_claim's result. Nothing was drafted. Run check_claim with these "
                      "IDs added, then draft or ask the reviewer.")


def calendar_only(check: dict | None, decided: dict | None = None) -> dict[str, list[str]]:
    """{"item:gap": [EVT ids]} for guarded rules the latest check passes on calendar events alone (no
    mailbox message among the item's evidence) and the reviewer did not decide."""
    out: dict[str, list[str]] = {}
    for i in (check or {}).get("items", []):
        used = list(i.get("evidence_used") or [])
        if not used or not all(str(e).startswith("EVT-") for e in used) or i.get("item_id") in (decided or {}):
            continue
        for f in i.get("findings", []):
            code = CALENDAR_GUARDED.get(f.get("rule"))
            if code and f.get("status") in ("ok", "partial"):
                out[f"{i['item_id']}:{code}"] = used
    return out
GAP_SEARCH_TOOL = "search_for_gap"  # version B only; wraps SEARCH_TOOL and attributes the search to one gap
EMPLOYEE_MESSAGE_TOOLS = frozenset({EVIDENCE_TOOL, "send_message"})
DECISION_TOOLS = frozenset({"check_claim", "draft_decision", EVIDENCE_TOOL})
EVIDENCE_ARG_TOOLS = DECISION_TOOLS
SEARCHES_PER_GAP = 2  # per open gap before asking the employee; 1 more after the ask (SKILL.md: 3 in all)
SEARCHES_AFTER_ASK = 1
MAX_BLOCKED = 2  # consecutive refused searches before the graph finishes the run itself
STABLE_IGNORE = frozenset({"belongs_to_other_employee", "memo_unsigned", "no_matching_item"})
SEARCH_LIMIT = ("search limit: {gap} has used its {n} mailbox searches{after}. Do not search for it again; "
                "{next}")
ALREADY_SEARCHED = ("already searched: an equivalent query ran earlier in this run and no message has gone to "
                    "the employee since, so the mailbox is unchanged. Do not repeat it.")
USE_GAP_SEARCH = ("several gaps are open ({gaps}) and this query doesn't name one, so it counts against none; "
                  "prefer search_for_gap(item_id, gap_code, query).")
UNATTRIBUTED_LIMIT = ("search limit: {n} mailbox searches that name no open gap. Use search_for_gap(item_id, "
                      "gap_code, query) for one of: {gaps}. Nothing was searched.")
GAP_WORDS = {
    "receipt_missing": r"receipt|kwitansi|struk|nota|invoice|bill",
    "receipt_amount_mismatch": r"receipt|kwitansi|struk|nota|invoice|bill|amount",
    "multiple_receipts": r"receipt|kwitansi|struk|nota|invoice|bill",
    "boarding_pass_missing": r"boarding|pass|e-?ticket|itinerary",
    "hotel_nights_mismatch": r"folio|nights?|check-?in|check-?out|stay",
}
CATEGORY_QUERY = {"hotel": r"hotel|room|folio", "taxi": r"taxi|cab|grab|gojek|bluebird|ride",
                  "flight": r"flight|plane|airline|garuda|lion|boarding", "meal": r"meal|dinner|lunch|food|restaurant",
                  "late_checkout": r"late|checkout|check-?out", "per_diem": r"per ?diem|allowance"}


def attribute_query(query: str, gaps: list[str], check: dict) -> str | None:
    """The one open gap a raw mailbox query is clearly about (gap words + item category), else None."""
    q = str(query).lower()
    cat = {i["item_id"]: i.get("category", "") for i in check.get("items", [])}
    scores = {}
    for k in gaps:
        item, code = k.split(":", 1)
        sc = bool(re.search(GAP_WORDS.get(code, r"$^"), q)) + bool(re.search(CATEGORY_QUERY.get(cat.get(item), r"$^"), q))
        scores[k] = sc + (1 if item.lower() in q else 0)
    best = max(scores.values(), default=0)
    top = [k for k, v in scores.items() if v == best]
    return top[0] if best > 0 and len(top) == 1 else None
NOT_FROM_REVIEWER = ("human_decisions must come from the reviewer, not from you. The reviewer's decisions so far "
                     "are {d}; omit human_decisions and the graph fills them in. Nothing ran. If a decision is "
                     "needed, ask the reviewer.")


class GapSearchArgs(BaseModel):
    item_id: str = Field(description="item ID from your latest check_claim, e.g. IT-2")
    gap_code: str = Field(description="one gap code that check_claim still reports for that item")
    query: str = Field(description="mailbox search words, e.g. 'boarding pass CGK UPG'")


def gap_search_tool() -> StructuredTool:
    async def _never(**_kw):  # run_tools handles every call; this body exists only for the schema
        raise RuntimeError("search_for_gap is handled by the graph")
    return StructuredTool.from_function(
        coroutine=_never, name=GAP_SEARCH_TOOL, args_schema=GapSearchArgs,
        description=("Search the reimburse@ mailbox (same results as search_reimburse_mailbox, for the claim's "
                     "owner) for evidence for ONE open gap from your latest check_claim. Each gap gets 2 searches "
                     "before you ask the employee and 1 more after."))


def search_key(args: dict) -> str:
    """Same employee + same set of query words (case, order and punctuation ignored)."""
    words = sorted(set(re.findall(r"[a-z0-9]+", str(args.get("query", "")).lower())))
    return json.dumps([args.get("employee_id"), words])


def open_gaps(check: dict | None) -> list[str]:
    if not check:
        return []
    return [f"{i['item_id']}:{g}" for i in check.get("items", []) if i.get("status") == "needs_evidence"
            for g in i.get("gaps", [])]


def check_position(messages, claim_id: str):
    """(index, tool_call_id, parsed) of the latest check_claim for claim_id, or None."""
    for n in range(len(messages) - 1, -1, -1):
        m = messages[n]
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "check_claim":
            try:
                data = json.loads(m.content)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and data.get("claim_id") == claim_id:
                return n, m.tool_call_id, data
    return None


# --- plain-language reviewer decisions -----------------------------------------------------------------
CATEGORY_WORDS = {
    "hotel": "hotel", "room": "hotel", "late checkout": "late_checkout", "late check-out": "late_checkout",
    "checkout": "late_checkout", "taxi": "taxi", "cab": "taxi", "flight": "flight", "ticket": "flight",
    "meal": "meal", "dinner": "meal", "lunch": "meal", "per diem": "per_diem", "per-diem": "per_diem",
}
ACTIONS = [  # every action a clause names is collected; a clause naming two different actions is ambiguous
    ("use", re.compile(r"\buse\s+(MSG-[\w-]+)", re.I)),
    ("reject", re.compile(r"\b(reject|deny|decline|don'?t pay|do not pay|tolak)\b", re.I)),
    ("approve_capped", re.compile(r"\b(cap|capped|at the cap|up to the cap|policy limit|plafon)\b", re.I)),
    ("approve_full", re.compile(r"\b(in full|full amount|approve[_ ]full|pay (it |the \w+ )?all|whole amount)\b", re.I)),
]
NEGATION = re.compile(r"\b(don'?t|do not|not|never|no|jangan|tidak)\b", re.I)
REJECT_PHRASES = re.compile(r"\b(don'?t pay|do not pay)\b", re.I)  # a reject action, not a negation
EXCEPT = re.compile(r"\b(except|but not|other than|apart from|kecuali|selain)\b", re.I)
ALTERNATIVE = re.compile(r"\b(or|atau|either)\b", re.I)
HUMAN_CHOICES = frozenset({"approve_full", "approve_capped", "reject"})  # = reimburse_buddy.rules.HUMAN_CHOICES
USE_CHOICE = re.compile(r"use:[A-Z]+-[\w-]+")


def valid_choice(v) -> bool:
    return isinstance(v, str) and (v in HUMAN_CHOICES or bool(USE_CHOICE.fullmatch(v)))


def parse_reviewer_turn(text: str, items: list[dict]) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Map a reviewer's words to {item_id: choice}, conservatively; returns (decisions, skipped, quotes) where
    quotes[item] is the reviewer's own words each decision came from (shown back for confirmation).
    Nothing here is stored directly: intake asks the reviewer to confirm first. Refused (-> skipped):
    - pasted alternatives ("approve the hotel in full; or cap it", "cap or reject"): two different actions
      plus "or" in one message means the reviewer is quoting options, not choosing;
    - negation anywhere in a clause with an action ("don't cap it");
    - "everything except ..." style sentences;
    - a sentence naming an item without an action next to one with an action ("cap IT-3 and IT-4");
    - item IDs that are not in the claim, and JSON values outside HUMAN_CHOICES / use:<ID>."""
    out: dict[str, str] = {}
    quotes: dict[str, str] = {}
    skipped: list[str] = []
    ids = {i.get("item_id") for i in items}
    for blob in re.findall(r"\{[^{}]*\}", text):
        try:
            d = json.loads(blob.replace("'", '"'))
        except ValueError:
            continue
        if isinstance(d, dict):
            for k, v in d.items():
                k = str(k).upper()
                if re.fullmatch(r"IT-\d+", k) and (not ids or k in ids) and valid_choice(v):
                    out[k], quotes[k] = v, blob
                else:
                    skipped.append(f"{k}: {v}")
    rest = re.sub(r"\{[^{}]*\}", " ", text)
    parsed: list[tuple[str, str, str]] = []  # (item, choice, clause)
    for sentence in re.split(r"[.;\n]", rest):
        if not sentence.strip():
            continue
        if EXCEPT.search(sentence) and any(rx.search(sentence) for _n, rx in ACTIONS):
            skipped.append(sentence.strip())
            continue
        found, bad = [], False
        for clause in re.split(r",\s*(?:and\s+)?|\band\b|\bthen\b|\bdan\b", sentence):
            if not clause.strip():
                continue
            acts = {name: m for name, rx in ACTIONS if (m := rx.search(clause))}
            named = {t.upper() for t in re.findall(r"\bIT-\d+\b", clause, re.I)}
            if not acts:
                if named:  # "... and IT-4": an item with no action of its own
                    bad = True
                continue
            if NEGATION.search(REJECT_PHRASES.sub(" ", clause)) or len(acts) > 1:
                skipped.append(clause.strip())
                continue
            name, m = next(iter(acts.items()))
            targets = named & ids if ids else named
            if named and not targets:  # an item ID this claim doesn't have
                skipped.append(clause.strip())
                continue
            if not targets:
                low = clause.lower()
                cats = {c for w, c in CATEGORY_WORDS.items() if re.search(rf"\b{re.escape(w)}\b", low)}
                rows = [i for i in items if i.get("category") in cats]
                waiting = [i for i in rows if i.get("status") == "needs_evidence"]
                rows = waiting if len(waiting) == 1 else rows
                if len(cats) == 1 and len(rows) == 1:
                    targets = {rows[0]["item_id"]}
            if len(targets) != 1:
                skipped.append(clause.strip())
                continue
            found.append((targets.pop(), f"use:{m.group(1).upper()}" if name == "use" else name, clause.strip()))
        if bad and found:
            skipped.append(sentence.strip())
            continue
        parsed.extend(found)
    named_actions = {name for name, rx in ACTIONS if rx.search(rest)}
    if (parsed or out) and ALTERNATIVE.search(rest) and len(named_actions | set(out.values())) > 1:
        return {}, [*skipped, text.strip()], {}  # quoted options, not a choice: ask instead
    for item, choice, q in parsed:
        if item in out and out[item] != choice:  # same item, two different choices: refuse both
            skipped.append(q)
            out.pop(item)
            quotes.pop(item, None)
            continue
        out[item], quotes[item] = choice, q
    return out, skipped, quotes


def parse_reviewer_decisions(text: str, items: list[dict]) -> tuple[dict[str, str], list[str]]:
    dec, skipped, _q = parse_reviewer_turn(text, items)
    return dec, skipped


# --- reviewer instructions the graph may carry out itself (args fully determined; always gated) -------
INSTRUCTIONS = [
    ("outcome_notice", re.compile(r"\b(send|tell|let|inform|notify|email|message)\b[^.?!\n]*\b(employee|claimant|karyawan)\b"
                                  r"[^.?!\n]*\b(outcome|result|decision|amount|keputusan)\b", re.I)),
    ("request_approval", re.compile(r"\b(escalate|request[_ ]approval|ask (the |their |his |her )?manager|manager'?s? approval)\b", re.I)),
    ("update_claim_status", re.compile(r"\bupdate_claim_status\b|\b(update|set|record|mark|change)\b[^.?!\n]*\bstatus\b", re.I)),
]
ENGINE_STATUS = {"approve": "approved", "partial_approve": "partially_approved", "reject": "rejected",
                 "needs_info": "on_hold"}
OUTCOME_SUBJECT = "Claim {cid}: outcome of your reimbursement claim"
OUTCOME_BODY = ("Hello,\n\nYour reimbursement claim {cid} has been reviewed. This is the finance decision note, "
                "exactly as the rule engine produced it:\n\n{draft}\n\nReimburse Buddy (sent after finance approved "
                "this message)")
GRAPH_TOOL_FOR = {"outcome_notice": "send_message", "request_approval": "request_approval",
                  "update_claim_status": "update_claim_status"}


def parse_instructions(text: str, items: list[dict], at: int) -> list[dict]:
    """Explicit reviewer instructions in one human message: [{"kind", "quote", "at", "item"?}]. A sentence
    with a negation ("don't message the employee") is no instruction."""
    out = []
    for sentence in re.split(r"(?<=[.?!])\s+|\n", text):
        if NEGATION.search(sentence):
            continue
        for kind, rx in INSTRUCTIONS:
            if kind in {o["kind"] for o in out} or not rx.search(sentence):
                continue
            ins = {"kind": kind, "quote": sentence.strip(), "at": at}
            if kind == "request_approval":
                named = {t.upper() for t in re.findall(r"\bIT-\d+\b", sentence, re.I)}
                if not named:
                    low = sentence.lower()
                    cats = {c for w, c in CATEGORY_WORDS.items() if re.search(rf"\b{re.escape(w)}\b", low)}
                    named = {i["item_id"] for i in items if i.get("category") in cats}
                if len(named) != 1:
                    continue  # whose approval? not determined: leave it to the model / reviewer
                ins["item"] = named.pop()
            out.append(ins)
    return out


def _jsonish(content):
    try:
        v = json.loads(content)
        return v if isinstance(v, dict) else None
    except (TypeError, ValueError):
        return None


def _with_note(content: str, note: str) -> str:
    d = _jsonish(content)
    if d is not None:
        return json.dumps({**d, "graph_note": note}, ensure_ascii=False)
    return f"{content}\n\ngraph_note: {note}"


def describe_writes(writes: list[dict]) -> str:
    if not writes:
        return "No writes were made in this run."
    parts = []
    for w in writes:
        a = w.get("args", {})
        bits = [str(a[k]) for k in ("claim_id", "item_id", "gap_code", "to", "status") if a.get(k)]
        parts.append(f"{w['tool']}({', '.join(bits)})" + (" [sent by the graph]" if w.get("by") == "graph" else ""))
    return "Writes made in this run: " + "; ".join(parts) + "."


log = logging.getLogger("reimburse_agent")


def message_usage(m) -> dict:
    """Provider-reported tokens of one model reply: input, cache_hit (DeepSeek prompt_cache_hit_tokens, or the
    OpenAI-style cached/cache_read count) and output. All 0 when the provider gave none."""
    tu = (getattr(m, "response_metadata", None) or {}).get("token_usage") or {}
    um = getattr(m, "usage_metadata", None) or {}
    inp = int(tu.get("prompt_tokens") or um.get("input_tokens") or 0)
    out = int(tu.get("completion_tokens") or um.get("output_tokens") or 0)
    hit = int(tu.get("prompt_cache_hit_tokens") or (tu.get("prompt_tokens_details") or {}).get("cached_tokens")
              or (um.get("input_token_details") or {}).get("cache_read") or 0)
    return {"input": inp, "cache_hit": min(hit, inp), "output": out}


def message_tokens(m) -> int:
    """Budgeted tokens of one model reply: cache-MISS input plus output (cache hits are ~10x cheaper on
    DeepSeek, so they do not count against a run's budget). 0 when the provider gave no usage."""
    u = message_usage(m)
    return u["input"] - u["cache_hit"] + u["output"]


def token_split(system_text: str, tools, served: dict | None) -> dict:
    """Rough (chars/4) size of what every model call carries: SKILL.md, the version B addendum, the bound
    tool schemas, and any served text. Logged once per build_graph."""
    schemas = []
    for t in tools:
        sch = t.args_schema if isinstance(t.args_schema, dict) else t.args_schema.model_json_schema()
        schemas.append({"name": t.name, "description": t.description, "parameters": sch})
    add = PROMPT_ADDENDUM if system_text.endswith(PROMPT_ADDENDUM) else ""
    return {"skill_md": (len(system_text) - len(add)) // 4, "addendum": len(add) // 4,
            "tool_schemas": len(json.dumps(schemas, default=str)) // 4, "tools": len(schemas),
            "served_text": sum(len(v[1]) for v in (served or {}).values()) // 4}


def estimate_tokens(msgs) -> int:
    """Rough chars/4 size of a prompt, for models (fakes) that report no usage."""
    return sum(len(str(getattr(m, "content", ""))) + len(json.dumps(getattr(m, "tool_calls", None) or [], default=str))
               for m in msgs) // 4


BUDGET_STOP = "graph_budget_stop"  # AIMessage.name of the graph's own stop text (no model call behind it)


def build_graph(model, tools, system_prompt: str | None = None, checkpointer=None, compact: bool = True,
                max_gated: int = MAX_GATED_CALLS, run_claim: str | None = None,
                served: dict[str, tuple[dict, str]] | None = None, token_budget: int | None = None,
                model_search: bool = True):
    """model: any LangChain chat model with bind_tools. tools: LangChain tools (e.g. from MCP).
    compact: shrink tool outputs before the model sees them (see compact.py); False = raw.
    run_claim: the claim this run investigates; without it every evidence request is gated, reviewer
    decisions are not injected and there is no gap search budget or auto-draft.
    served: tool name -> (args it holds, output) the caller already has (batch: triage_claims, get_policy). The tool stays
    in the bound list; a call to it just returns the held text instead of hitting the server again.
    token_budget: model tokens this run may use; once spent, the agent node stops and the graph drafts from
    the engine's check with zero model tokens ("needs human: model budget used; open gaps: ...").
    model_search=False: the model is not given the mailbox search tools (the graph's own post-ask reply search
    still runs); an eval switch to measure what the graph's search does on its own."""
    system = SystemMessage(system_prompt if system_prompt is not None else load_system_prompt())
    tools = [t for t in tools if t.name not in HIDDEN_TOOLS]
    by_name = {t.name: t for t in tools}
    if run_claim and SEARCH_TOOL in by_name and GAP_SEARCH_TOOL not in by_name:
        tools = [*tools, gap_search_tool()]  # bound once for the whole run: the tool list never changes
        by_name[GAP_SEARCH_TOOL] = tools[-1]
    model_tools = tools if model_search else [t for t in tools if t.name not in (SEARCH_TOOL, GAP_SEARCH_TOOL)]
    bound = model.bind_tools(model_tools)
    split = token_split(system.content, model_tools, served)
    log.info("token split (est. tokens per call): %s", split)

    async def invoke(name: str, args: dict) -> str:
        if served and name in served:
            want, text = served[name]  # held only for these args (e.g. the same batch); other args go to the server
            if all(args.get(k) in (None, v) for k, v in want.items()):
                return text
        res = await by_name[name].ainvoke(args)
        if compact:
            return _compact(tool_text(res))
        return res if isinstance(res, str) else json.dumps(res, default=str, ensure_ascii=False)

    async def intake(state: State):
        """Human channel: parse decisions out of the newest reviewer message, confirm them with the reviewer
        (interrupt quoting their own words) before storing; note explicit instructions; mark searched gaps
        as 'reviewer asked' when the reviewer answers an agent that stopped."""
        msgs = state["messages"]
        last = msgs[-1] if msgs else None
        if not isinstance(last, HumanMessage):
            return {}
        out: dict = {}
        if state.get("decisions_source"):
            out["decisions_source"] = ""  # structured decisions arrived with this turn: nothing to parse
        check = latest_check(msgs, run_claim) if run_claim else None
        items = (check or {}).get("items", [])
        text = str(last.content)
        if run_claim and not items and re.search(r"\b(cap|reject|approve|use|hotel|taxi|flight|escalate|manager)\b",
                                                 text, re.I) and "check_claim" in by_name:
            try:
                items = (_jsonish(await invoke("check_claim", {"claim_id": run_claim})) or {}).get("items", [])
            except Exception:
                items = []
        if run_claim:
            new = [i for i in parse_instructions(text, items, len(msgs) - 1)
                   if GRAPH_TOOL_FOR[i["kind"]] in by_name]
            if new:
                out["instructions"] = [*state.get("instructions", []), *new]
        if run_claim and len(msgs) > 1:
            dec, skipped, quotes = ({}, [], {}) if state.get("decisions_source") else parse_reviewer_turn(text, items)
            if dec:
                lines = [f'Apply {k} -> {v}? from: "{quotes.get(k, "")}"' for k, v in dec.items()]
                answer = interrupt({"tool": "confirm_reviewer_decisions", "args": {"decisions": dec},
                                    "tool_call_id": f"confirm-decisions-{len(msgs) - 1}", "by": "graph",
                                    "from": text, "prompt": "\n".join(lines)})
                ok = (isinstance(answer, dict) and answer.get("approved") is True
                      and answer.get("tool_call_id") == f"confirm-decisions-{len(msgs) - 1}")
                if ok:
                    out["reviewer_decisions"] = dec
                else:
                    skipped = [*skipped, *(f"not confirmed: {line}" for line in lines)]
            if skipped:
                out["unparsed_reviewer"] = [*state.get("unparsed_reviewer", []), *skipped]
            att = {k: dict(v) for k, v in state.get("gap_attempts", {}).items()}
            for k in open_gaps(check):
                if att.get(k, {}).get("search") is not None:
                    att[k]["reviewer"] = len(msgs) - 1
            out["gap_attempts"] = att
        return out

    async def agent(state: State):
        used = state.get("tokens_used", 0)
        if token_budget is not None and used >= token_budget:
            return {"messages": [AIMessage(name=BUDGET_STOP, content=(
                f"Stopped: this claim run used its model budget ({used} of {token_budget} tokens)."))]}
        prompt = [system, *state["messages"]]
        reply = await bound.ainvoke(prompt)
        n = message_tokens(reply) or estimate_tokens(prompt) + estimate_tokens([reply])
        return {"messages": [reply], "tokens_used": used + n}

    def gate(state: State):
        last = state["messages"][-1]
        declined: dict[str, str] = {}
        count = state.get("gated_count", 0)
        sigs = list(state.get("declined_sigs", []))
        free = [list(x) for x in state.get("free_evidence", [])]
        for call in last.tool_calls:
            if call["name"] not in by_name:
                continue  # unknown or hidden tool: run_tools returns an error, no prompt for the reviewer
            if call["name"] == EVIDENCE_TOOL and free_evidence_request(call["args"], run_claim, state["messages"], free):
                a = call["args"]
                free.append([a["claim_id"], a["item_id"], a["gap_code"]])
                continue
            if not needs_confirmation(call["name"], call["args"]):
                continue
            sig = call_signature(call)
            if sig in sigs:
                declined[call["id"]] = "identical call was already declined by the reviewer; not asking again"
                continue
            if count >= max_gated:
                declined[call["id"]] = f"limit of {max_gated} approval requests per run reached"
                continue
            count += 1
            answer = interrupt({"tool": call["name"], "args": call["args"], "tool_call_id": call["id"]})
            # The answer must name the call it approves; anything else counts as a decline.
            ok = (isinstance(answer, dict) and answer.get("approved") is True
                  and answer.get("tool_call_id") == call["id"])
            if not ok:
                reason = (answer.get("reason") if isinstance(answer, dict) else "") or "no reason given"
                declined[call["id"]] = reason
                sigs.append(sig)
        return {"declined": declined, "gated_count": count, "declined_sigs": sigs, "free_evidence": free}

    def trusted_decisions(state: State, claim_id) -> dict:
        return dict(state.get("reviewer_decisions", {})) if run_claim and claim_id == run_claim else {}

    def gap_limit(att: dict) -> int:
        asked = att.get("request") is not None or att.get("reviewer") is not None
        return SEARCHES_PER_GAP + (SEARCHES_AFTER_ASK if asked else 0)

    def guard_pending(check: dict | None, attempts: dict, state: State) -> dict[str, list[str]]:
        """Calendar-only resolutions with no mailbox search attributed to that gap yet."""
        if not run_claim or SEARCH_TOOL not in by_name:
            return {}
        return {k: v for k, v in calendar_only(check, trusted_decisions(state, run_claim)).items()
                if not attempts.get(k, {}).get("searches")}

    async def corroborate(state: State, check: dict, attempts: dict, pending: dict[str, list[str]], ev: list[str],
                          ignored: dict, here: int) -> tuple[list[str], dict | None]:
        """One mailbox search per pending gap, built from the calendar event's own title (state only, no
        tool-call message); check_claim decides whether any hit matters. Returns (new IDs check_claim kept,
        the re-check) when the outcome changed, else ([], None)."""
        emp, found = check.get("employee_id"), []
        for k, evts in pending.items():
            item = k.split(":", 1)[0]
            row = next((i for i in check.get("items", []) if i.get("item_id") == item), {})
            titles = []
            if "get_calendar" in by_name:
                day = next((f.get("params", {}).get("date") for f in row.get("findings", []) if f.get("params")), None)
                try:
                    evs = _rows(await invoke("get_calendar", {"employee_id": emp, "date_from": day or "0000-01-01",
                                                                 "date_to": day or "9999-12-31"}))
                    titles = [e.get("title", "") for e in evs or [] if isinstance(e, dict) and e.get("id") in evts]
                except Exception:
                    titles = []
            query = " ".join(titles) or f"{row.get('category', '')} {item}"
            try:
                hits = _rows(await invoke(SEARCH_TOOL, {"employee_id": emp, "query": query}))
            except Exception:
                hits = []
            a = attempts.setdefault(k, {})
            a["searches"], a["search"], a["calendar_check"] = a.get("searches", 0) + 1, here, query
            found += [h["id"] for h in hits if isinstance(hits, list) and isinstance(h, dict) and h.get("id")
                      and h["id"] not in found and h["id"] not in ev and h["id"] not in ignored]
        if not found:
            return [], None
        base = {"claim_id": run_claim}
        hd = trusted_decisions(state, run_claim)
        if hd:
            base["human_decisions"] = hd
        try:
            before = _jsonish(await invoke("check_claim", {**base, **({"evidence_ids": ev} if ev else {})})) or {}
            after = _jsonish(await invoke("check_claim", {**base, "evidence_ids": [*ev, *found]})) or {}
        except Exception:
            return [], None
        def outcome(d):
            return (d.get("decision"), d.get("total_approvable"),
                    [(i.get("item_id"), i.get("status")) for i in d.get("items", [])])
        if outcome(before) == outcome(after):
            return [], None
        # The hits changed the engine's result. A meeting update can be listed under ignored_evidence (it
        # matches no item itself) and still move the meeting, so every hit is reported, not only matched ones.
        return found, after

    async def run_tools(state: State):
        last = state["messages"][-1]
        out = []
        cache = dict(state.get("search_cache", {}))
        attempts = {k: dict(v) for k, v in state.get("gap_attempts", {}).items()}
        here = len(state["messages"])
        streak = state.get("blocked_streak", 0)
        unattr = state.get("unattributed_searches", 0)
        writes = list(state.get("writes", []))
        ignored = dict(state.get("ignored_ids", {}))
        effective = dict(state.get("effective_args", {}))
        free = [list(x) for x in state.get("free_evidence", [])]
        check = latest_check(state["messages"], run_claim) if run_claim else None
        gaps_now = open_gaps(check)

        def record_write(name, args, by="model"):
            writes.append({"tool": name, "args": args, "by": by})
            if name in EMPLOYEE_MESSAGE_TOOLS:
                nonlocal cache
                cache = {}  # a reply may land in the mailbox now
            if name == EVIDENCE_TOOL:
                k = f"{args.get('item_id')}:{args.get('gap_code')}"
                attempts.setdefault(k, {})["request"] = here
            elif name == "send_message":
                for k in gaps_now:
                    attempts.setdefault(k, {})["request"] = here

        async def search(args, gap: str | None) -> str:
            nonlocal streak
            key = search_key(args)
            if key in cache:
                streak += 1
                return json.dumps({"note": ALREADY_SEARCHED, "results": json.loads(cache[key])}, ensure_ascii=False)
            if gap is not None:
                att = attempts.setdefault(gap, {})
                lim = gap_limit(att)
                if att.get("searches", 0) >= lim:
                    streak += 1
                    return json.dumps({"note": await on_limit(gap, lim)}, ensure_ascii=False)
            content = await invoke(SEARCH_TOOL, args)
            streak = 0
            cache[key] = json.dumps(_as_json(content), ensure_ascii=False)
            if gap is not None:
                attempts[gap]["searches"] = attempts[gap].get("searches", 0) + 1
                attempts[gap]["search"] = here
            return content

        def budget_used(k):
            a = attempts.get(k, {})
            return a.get("searches", 0) >= gap_limit(a)

        async def on_limit(gap: str, lim: int) -> str:
            """A refused search. When the run is stuck (MAX_BLOCKED refusals in a row, or every open gap has
            used its budget), the graph itself sends the fixed-template request for each free document gap
            not yet requested, so the employee is asked exactly once per gap."""
            after = " (including the one after asking the employee)" if lim > SEARCHES_PER_GAP else ""
            stuck = streak >= MAX_BLOCKED or all(budget_used(k) for k in gaps_now)
            sent = []
            if stuck and EVIDENCE_TOOL in by_name:
                for k in gaps_now:
                    item, code = k.split(":", 1)
                    if code not in FREE_EVIDENCE_GAPS or attempts.get(k, {}).get("request") is not None:
                        continue
                    a = {"claim_id": run_claim, "item_id": item, "gap_code": code}
                    if not free_evidence_request(a, run_claim, state["messages"], free):
                        continue
                    full = {**a, "evidence_ids": evidence_now() or None,
                            "human_decisions": trusted_decisions(state, run_claim) or None}
                    try:
                        res = _jsonish(await invoke(EVIDENCE_TOOL, {k2: v for k2, v in full.items() if v is not None}))
                    except Exception:
                        res = None
                    if res is not None and "error" not in res:
                        free.append([run_claim, item, code])
                        record_write(EVIDENCE_TOOL, a, by="graph")
                        sent.append(k)
            if sent:
                return SEARCH_LIMIT.format(gap=gap, n=lim, after=after, next=(
                    f"the graph has now asked the employee for {', '.join(sent)} (fixed template). "
                    "You may search once more per requested gap, then run check_claim."))
            return SEARCH_LIMIT.format(gap=gap, n=lim, after=after, next=(
                "run check_claim and draft the decision, or ask the reviewer."))

        def evidence_now() -> list[str]:
            pos = check_position(state["messages"], run_claim) if run_claim else None
            if not pos:
                return []
            return list(effective.get(pos[1], {}).get("evidence_ids") or [])

        reply_found: list[str] = list(state.get("reply_evidence", []))

        async def presearch_draft(args: dict) -> str | None:
            """Before a MODEL-made draft: a document gap the employee was asked about whose post-ask search is
            unused may already have its reply in the mailbox. The graph runs that one search itself (state
            only, no tool-call message), re-checks with any hits, and holds the draft if the outcome changed."""
            ev0 = [e for e in (args.get("evidence_ids") or []) if e not in ignored]
            drafted = check  # what the engine says for exactly the draft's evidence
            if any(str(e).startswith("EVT-") for e in ev0):
                try:
                    drafted = _jsonish(await invoke("check_claim", {**args, "evidence_ids": ev0})) or check
                except Exception:
                    drafted = check
            pend = guard_pending(drafted, attempts, state)
            if pend:
                keep, _after = await corroborate(state, drafted, attempts, pend, ev0, ignored, here)
                if keep:
                    return json.dumps({"error": "new_evidence_found", "message": CALENDAR_ONLY_NOTE.format(
                        k=", ".join(pend), ids=", ".join(keep)), "new_evidence_ids": keep}, ensure_ascii=False)
            waiting = awaiting_reply({**state, "gap_attempts": attempts}, gaps_now)
            if not waiting or SEARCH_TOOL not in by_name:
                return None
            emp, found = check.get("employee_id"), []
            for k in waiting:
                item = k.split(":", 1)[0]
                try:
                    hits = _rows(await invoke(SEARCH_TOOL, {"employee_id": emp, "query": f"{run_claim} {item}"}))
                except Exception:
                    hits = []
                attempts.setdefault(k, {})["searches"] = attempts.get(k, {}).get("searches", 0) + 1
                attempts[k]["search"] = here
                found += [h["id"] for h in hits if isinstance(hits, list) and isinstance(h, dict) and h.get("id")
                          and h["id"] not in found]
            ev = [e for e in (args.get("evidence_ids") or []) if e not in ignored]
            new = [e for e in found if e not in ev]
            if not new:
                return None
            base = {k2: v for k2, v in args.items() if k2 != "evidence_ids"}
            try:
                before = _jsonish(await invoke("check_claim", {**base, **({"evidence_ids": ev} if ev else {})})) or {}
                after = _jsonish(await invoke("check_claim", {**base, "evidence_ids": [*ev, *new]})) or {}
            except Exception:
                return None
            bad = {r.get("id") for r in after.get("ignored_evidence") or [] if isinstance(r, dict)}
            keep = [e for e in new if e not in bad]

            def outcome(d):
                return (d.get("decision"), d.get("total_approvable"),
                        [(i.get("item_id"), i.get("status")) for i in d.get("items", [])])
            if not keep or outcome(before) == outcome(after):
                return None
            reply_found.extend(e for e in keep if e not in reply_found)
            return json.dumps({"error": "new_evidence_found", "message": (
                "new evidence found; re-check before drafting. The graph searched the mailbox for the employee's "
                f"reply and found {', '.join(keep)}, which changes check_claim's result. Nothing was drafted. Run "
                "check_claim with these IDs added, then draft."), "new_evidence_ids": keep}, ensure_ascii=False)

        for call in last.tool_calls:
            name, args = call["name"], dict(call.get("args") or {})
            note, refused = None, False
            if call["id"] in state.get("declined", {}):
                content = json.dumps({"error": "reviewer_declined", "reason": state["declined"][call["id"]]})
            elif name not in by_name:  # includes hidden tools like reset_demo
                content = json.dumps({"error": f"unknown tool {name}"})
            elif name == GAP_SEARCH_TOOL:
                k = f"{args.get('item_id')}:{args.get('gap_code')}"
                if not check:
                    content = json.dumps({"error": "run check_claim first; search_for_gap needs an open gap from it"})
                elif k not in gaps_now:
                    streak += 1
                    content = json.dumps({"error": f"{k} is not an open gap in the latest check_claim",
                                          "open_gaps": gaps_now})
                else:
                    args = {"employee_id": check.get("employee_id"), "query": args.get("query", "")}
                    content = await search(args, k)
            elif name == SEARCH_TOOL:
                gap = None
                if run_claim and gaps_now:
                    gap = gaps_now[0] if len(gaps_now) == 1 else attribute_query(args.get("query", ""), gaps_now, check)
                if run_claim and len(gaps_now) > 1 and gap is None:
                    if search_key(args) not in cache and unattr >= SEARCHES_PER_GAP * len(gaps_now):
                        streak += 1
                        content = json.dumps({"note": UNATTRIBUTED_LIMIT.format(n=unattr, gaps=", ".join(gaps_now))})
                    else:
                        before = len(cache)
                        content = await search(args, None)
                        unattr += len(cache) > before
                        note = USE_GAP_SEARCH.format(gaps=", ".join(gaps_now))
                else:
                    content = await search(args, gap)
            else:
                content = None
                if name in DECISION_TOOLS:
                    trusted = trusted_decisions(state, args.get("claim_id"))
                    model_hd = args.get("human_decisions") or {}
                    if model_hd and model_hd != trusted:
                        refused = True
                        content = json.dumps({"error": "human_decisions_not_from_reviewer",
                                              "message": NOT_FROM_REVIEWER.format(d=json.dumps(trusted))})
                    else:
                        args.pop("human_decisions", None)
                        if trusted:
                            args["human_decisions"] = trusted
                    ev = list(args.get("evidence_ids") or [])
                    drop = [e for e in ev if e in ignored]
                    if drop:
                        args["evidence_ids"] = [e for e in ev if e not in ignored]
                        note = ("removed " + ", ".join(f"{e} ({ignored[e]})" for e in drop)
                                + ": check_claim already ignored them in this run. Don't pass them again.")
                if content is None and name == "draft_decision" and args.get("claim_id") == run_claim and check:
                    hold = await presearch_draft(args)
                    if hold:
                        content, refused = hold, True
                if content is None:
                    try:
                        content = await invoke(name, args)
                        res = _jsonish(content)
                        failed = res is not None and "error" in res
                        if name == "check_claim" and res:
                            for row in res.get("ignored_evidence") or []:
                                if row.get("reason") in STABLE_IGNORE:
                                    ignored[row["id"]] = row["reason"]
                        if (needs_confirmation(name) or name == EVIDENCE_TOOL) and not failed:
                            record_write(name, args)
                    except Exception as exc:  # surface tool errors to the model, don't crash the run
                        content = json.dumps({"error": str(exc)})
            if not refused:
                effective[call["id"]] = args
            if note:
                content = _with_note(content, note)
            out.append(ToolMessage(content=content, tool_call_id=call["id"], name=name))
        return {"messages": out, "declined": {}, "search_cache": cache, "gap_attempts": attempts,
                "blocked_streak": streak, "unattributed_searches": unattr, "writes": writes, "ignored_ids": ignored, "effective_args": effective,
                "free_evidence": free, "reply_evidence": reply_found}

    def exhausted(state: State, gaps: list[str], n: int) -> bool:
        att = state.get("gap_attempts", {})
        for k in gaps:
            a = att.get(k, {})
            asked = a.get("request") if k.split(":", 1)[1] in FREE_EVIDENCE_GAPS else a.get("reviewer")
            if a.get("search") is None or a["search"] >= n or asked is None or asked >= n:
                return False
        return True

    def awaiting_reply(state: State, gaps: list[str]) -> list[str]:
        """Document gaps the employee was asked about with no search since the ask and the post-ask search
        still unused: the reply may already be in the mailbox, so the gap is not exhausted yet."""
        att = state.get("gap_attempts", {})
        out = []
        for k in gaps:
            a = att.get(k, {})
            if k.split(":", 1)[1] not in FREE_EVIDENCE_GAPS or a.get("request") is None:
                continue
            if (a.get("search") is None or a["search"] < a["request"]) and a.get("searches", 0) < gap_limit(a):
                out.append(k)
        return out

    async def reply_search(state: State):
        """The graph's own post-ask search (outside model history: no tool-call message, just state): one
        mailbox search per awaited gap for "<claim> <item>", then check_claim decides which hits are real."""
        n, check_id, data = check_position(state["messages"], run_claim)
        att = {k: dict(v) for k, v in state.get("gap_attempts", {}).items()}
        emp = data.get("employee_id")
        found: list[str] = []
        here = len(state["messages"])
        for k in awaiting_reply(state, open_gaps(data)):
            item = k.split(":", 1)[0]
            try:
                hits = _rows(await invoke(SEARCH_TOOL, {"employee_id": emp, "query": f"{run_claim} {item}"}))
            except Exception:
                hits = []
            att[k]["searches"] = att[k].get("searches", 0) + 1
            att[k]["search"] = here
            found += [h["id"] for h in hits if isinstance(h, dict) and h.get("id") and h["id"] not in found]
        ignored = dict(state.get("ignored_ids", {}))
        prev = [e for e in (state.get("effective_args", {}).get(check_id, {}).get("evidence_ids") or []) if e not in ignored]
        keep: list[str] = []
        if found:
            try:
                fresh = _jsonish(await invoke("check_claim", {"claim_id": run_claim, "evidence_ids": [*prev, *found]})) or {}
            except Exception:
                fresh = {}
            bad = {r.get("id") for r in fresh.get("ignored_evidence", []) or [] if isinstance(r, dict)}
            keep = [e for e in found if e not in bad and e not in prev]
        return {"gap_attempts": att, "reply_evidence": [*state.get("reply_evidence", []), *keep]}

    async def corroborate_node(state: State):
        """Before the graph drafts on a calendar-only resolution: search the mailbox for that gap once. If a
        hit changes check_claim, stop and ask the reviewer (plain text, no tool-call message), else draft."""
        _n, check_id, data = check_position(state["messages"], run_claim)
        att = {k: dict(v) for k, v in state.get("gap_attempts", {}).items()}
        ignored = dict(state.get("ignored_ids", {}))
        ev = [e for e in (state.get("effective_args", {}).get(check_id, {}).get("evidence_ids") or []) if e not in ignored]
        pend = guard_pending(data, att, state)
        keep, after = await corroborate(state, data, att, pend, ev, ignored, len(state["messages"]))
        out: dict = {"gap_attempts": att}
        if keep:
            gaps = open_gaps(after)
            out["reply_evidence"] = [*state.get("reply_evidence", []), *keep]
            out["messages"] = [AIMessage(content=(
                f"Not drafted: {', '.join(pend)} rested on calendar events alone, and a mailbox search for it found "
                f"{', '.join(keep)}, which changes the engine's result (open now: {', '.join(gaps) or 'none'}). "
                "Needs your decision: how should these items be treated?"))]
        return out

    def after_corroborate(state: State):
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and not last.tool_calls and str(last.content).startswith("Not drafted:"):
            return END
        return "auto_draft"

    def after_tools(state: State):
        """To a draft once the latest check is clean, once every open gap was searched for and asked about
        (employee for document gaps, reviewer for the rest) before a check that still shows it, or once
        the agent keeps hitting refused searches with nothing left for the graph to request."""
        if not run_claim or "draft_decision" not in by_name:
            return "agent"
        pos = check_position(state["messages"], run_claim)
        if not pos or pos[1] in state.get("auto_drafted", []):
            return "agent"
        n, _cid, data = pos
        if any(getattr(m, "type", "") == "tool" and m.name == "draft_decision" for m in state["messages"][n:]):
            return "agent"
        if "error" in data:
            return "agent"
        gaps = open_gaps(data)
        if guard_pending(data, state.get("gap_attempts", {}), state):
            return "corroborate"
        if not gaps:
            return "auto_draft"
        if exhausted(state, gaps, n):
            return "reply_search" if SEARCH_TOOL in by_name and awaiting_reply(state, gaps) else "auto_draft"
        if state.get("blocked_streak", 0) >= MAX_BLOCKED:
            att = state.get("gap_attempts", {})
            left = [k for k in gaps if k.split(":", 1)[1] in FREE_EVIDENCE_GAPS and att.get(k, {}).get("request") is None]
            if not left:
                return "auto_draft"
        return "agent"

    async def auto_draft(state: State):
        """Re-check and draft with the latest check's evidence and the reviewer's decisions from state (never
        model-supplied decisions), then END the turn with a deterministic closing message built from the
        writes that actually ran. No synthetic tool-call message is inserted and no model call follows."""
        budget_hit = isinstance(state["messages"][-1], AIMessage) and state["messages"][-1].name == BUDGET_STOP
        pos = check_position(state["messages"], run_claim)
        if pos is None:  # budget spent before any check: the engine checks with no evidence
            try:
                pos = (None, "budget-no-check", _jsonish(await invoke("check_claim", {"claim_id": run_claim})) or {})
            except Exception as exc:
                pos = (None, "budget-no-check", {"error": str(exc)})
        _n, check_id, _data = pos
        ev = [e for e in (state.get("effective_args", {}).get(check_id, {}).get("evidence_ids") or [])
              if e not in state.get("ignored_ids", {})]
        ev += [e for e in state.get("reply_evidence", []) if e not in ev]  # replies the graph's own search found
        hd = trusted_decisions(state, run_claim)
        args = {"claim_id": run_claim, **({"evidence_ids": ev} if ev else {}), **({"human_decisions": hd} if hd else {})}
        try:
            fresh = _jsonish(await invoke("check_claim", args)) or {}
        except Exception:
            fresh = {}
        try:
            res = await by_name["draft_decision"].ainvoke(args)
            draft = tool_text(res) if not isinstance(res, str) else res
        except Exception as exc:
            draft = json.dumps({"error": str(exc)})
        gaps = open_gaps(fresh) if fresh else open_gaps(_data)
        need_reviewer = [k for k in gaps if k.split(":", 1)[1] not in FREE_EVIDENCE_GAPS]
        head = (f"needs human: model budget used; open gaps: {', '.join(gaps) or 'none'}. The graph drafted from "
                "the engine's check (no model call)." if budget_hit else
                "check_claim shows no open gaps, so the decision note was drafted." if not gaps else
                f"Drafted with these items still open: {', '.join(gaps)}. Every open gap was searched for, and "
                "asked about where possible." + (f" Still needs your decision: {', '.join(need_reviewer)}."
                                                 if need_reviewer else ""))
        if hd:
            head += f" Reviewer decisions applied: {json.dumps(hd)}."
        writes = list(state.get("writes", []))
        closing = AIMessage(content=f"{head}\n\n{draft}\n\n{describe_writes(writes)} Tell me if you want any change.")
        rec = {"args": args, "content": draft, "open_gaps": gaps, "check_id": check_id, "writes": writes,
               "budget": budget_hit}
        return {"messages": [closing], "auto_drafted": [*state.get("auto_drafted", []), check_id],
                "auto_drafts": [*state.get("auto_drafts", []), rec]}

    async def build_action(ins: dict, fresh: dict, args: dict) -> tuple[str | None, dict | str]:
        """(tool, args) when the instruction's arguments are fully determined by the engine/claim data,
        else (None, reason). The graph never writes free text: the outcome notice is a fixed template
        around draft_decision's output, so every amount comes from the engine."""
        kind, cid = ins["kind"], run_claim
        if kind == "outcome_notice":
            if fresh.get("decision") in (None, "needs_info"):
                return None, "the claim is not decided yet (check_claim: needs_info)"
            res = await by_name["draft_decision"].ainvoke(args)
            draft = tool_text(res) if not isinstance(res, str) else res
            if (_jsonish(draft) or {}).get("error"):
                return None, "draft_decision failed"
            return "send_message", {"to": fresh.get("employee_id"), "subject": OUTCOME_SUBJECT.format(cid=cid),
                                    "body": OUTCOME_BODY.format(cid=cid, draft=draft), "claim_id": cid}
        if kind == "request_approval":
            claim = _jsonish(await invoke("get_claim", {"claim_id": cid})) if "get_claim" in by_name else None
            mgr = ((claim or {}).get("employee") or {}).get("manager_id")
            if not mgr:
                return None, "the claimant's manager is not known"
            return "request_approval", {"manager_id": mgr, "claim_id": cid, "item_id": ins["item"],
                                        "reason": f'Reviewer asked: "{ins["quote"]}"'}
        status = ENGINE_STATUS.get(fresh.get("decision"))
        if not status:
            return None, "check_claim gave no decision"
        return "update_claim_status", {"claim_id": cid, "status": status,
                                       "note": f'Status from check_claim ({fresh.get("decision")}); reviewer asked: "{ins["quote"]}"'}

    def already_proposed(state: State, name: str, call_args: dict, since: int) -> bool:
        """Dedupe: same call_signature already declined or run, or the model itself proposed the same kind of
        action for this claim (and item) after the instruction."""
        sig = call_signature({"name": name, "args": call_args})
        if sig in state.get("declined_sigs", []):
            return True
        key = (name, call_args.get("claim_id"), call_args.get("item_id"))
        if any((w["tool"], w["args"].get("claim_id"), w["args"].get("item_id")) == key for w in state.get("writes", [])):
            return True
        for m in state["messages"][since:]:
            for c in getattr(m, "tool_calls", None) or []:
                a = c.get("args") or {}
                if c["name"] == name and (a.get("claim_id") in (run_claim, None, "")) and \
                        (name != "request_approval" or a.get("item_id") == call_args.get("item_id")):
                    return True
        return False

    async def graph_actions(state: State):
        """Carry out explicit reviewer instructions whose arguments are now fully determined. Each one is
        gated (interrupt quoting the reviewer's sentence), runs via invoke() OUTSIDE the model history (no
        AIMessage with tool_calls is ever inserted) and leaves a plain-text note."""
        pend = [dict(i) for i in state.get("instructions", [])]
        if not pend:
            return {}
        pos = check_position(state["messages"], run_claim)
        ev = [e for e in (state.get("effective_args", {}).get(pos[1], {}).get("evidence_ids") or [])
              if e not in state.get("ignored_ids", {})] if pos else []
        hd = trusted_decisions(state, run_claim)
        args = {"claim_id": run_claim, **({"evidence_ids": ev} if ev else {}), **({"human_decisions": hd} if hd else {})}
        try:
            fresh = _jsonish(await invoke("check_claim", args)) or {}
        except Exception:
            fresh = {}
        notes, keep = [], []
        writes = list(state.get("writes", []))
        sigs = list(state.get("declined_sigs", []))
        props = list(state.get("graph_proposals", []))
        count = state.get("gated_count", 0)
        for ins in pend:
            name, call_args = await build_action(ins, fresh, args)
            if name is None:
                keep.append(ins)  # not determined yet; try again at the end of a later turn
                continue
            if already_proposed({**state, "writes": writes, "declined_sigs": sigs}, name, call_args, ins.get("at", 0)):
                continue
            if count >= max_gated:
                notes.append(f"Not proposed: {name} (limit of {max_gated} approval requests reached).")
                continue
            count += 1
            tcid = f"graph-{name}-{len(props)}"
            answer = interrupt({"tool": name, "args": call_args, "tool_call_id": tcid, "by": "graph",
                                "from": ins["quote"], "prompt": f'Proposed by the graph because you asked: "{ins["quote"]}"'})
            ok = isinstance(answer, dict) and answer.get("approved") is True and answer.get("tool_call_id") == tcid
            rec = {"tool": name, "args": call_args, "id": tcid, "approved": ok, "from": ins["quote"]}
            if ok:
                try:
                    res = _jsonish(await invoke(name, call_args))
                    failed = res is not None and "error" in res
                except Exception as exc:
                    res, failed = {"error": str(exc)}, True
                rec["error"] = bool(failed)
                if failed:
                    notes.append(f"{name} for {run_claim} failed: {(res or {}).get('error')}")
                else:
                    writes.append({"tool": name, "args": call_args, "by": "graph_action"})
                    notes.append(f'Done: {name} for {run_claim} (you asked: "{ins["quote"]}").')
            else:
                reason = (answer.get("reason") if isinstance(answer, dict) else "") or "no reason given"
                sigs.append(call_signature({"name": name, "args": call_args}))
                notes.append(f"Not done: {name} for {run_claim}; you declined it ({reason}).")
            props.append(rec)
        out = {"instructions": keep, "writes": writes, "declined_sigs": sigs, "graph_proposals": props,
               "gated_count": count}
        if notes:
            out["messages"] = [AIMessage(content=" ".join(notes))]
        return out

    def route(state: State):
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "gate"
        if isinstance(last, AIMessage) and last.name == BUDGET_STOP and run_claim and "draft_decision" in by_name:
            pos = check_position(state["messages"], run_claim)
            drafted = pos and (pos[1] in state.get("auto_drafted", []) or any(
                getattr(m, "type", "") == "tool" and m.name == "draft_decision" for m in state["messages"][pos[0]:]))
            if not drafted:
                return "auto_draft"  # zero model tokens: the engine drafts with what the run found
        return "graph_actions" if run_claim and state.get("instructions") else END

    g = StateGraph(State)
    g.add_node("intake", intake)
    g.add_node("agent", agent)
    g.add_node("gate", gate)
    g.add_node("tools", run_tools)
    g.add_node("auto_draft", auto_draft)
    g.add_node("reply_search", reply_search)
    g.add_node("corroborate", corroborate_node)
    g.add_conditional_edges("corroborate", after_corroborate, ["auto_draft", END])
    g.add_edge("reply_search", "auto_draft")
    g.add_node("graph_actions", graph_actions)
    g.add_edge(START, "intake")
    g.add_edge("intake", "agent")
    g.add_conditional_edges("agent", route, ["gate", "graph_actions", "auto_draft", END])
    g.add_edge("gate", "tools")
    g.add_conditional_edges("tools", after_tools, ["auto_draft", "reply_search", "corroborate", "agent"])
    g.add_conditional_edges("auto_draft", lambda s: "graph_actions" if s.get("instructions") else END,
                            ["graph_actions", END])
    g.add_edge("graph_actions", END)
    compiled = g.compile(checkpointer=checkpointer)
    compiled.token_split = split
    return compiled
