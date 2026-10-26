"""Batch mode (version B): one parent run per batch, one fresh sub-run per exception claim.

The parent does triage and the reviewer-gated bulk approval itself (the reviewer's request is the
instruction; the arguments come from triage, so they are fully determined). Each exception claim then
gets its own graph run with run_claim set and an empty message history, on the same MCP session, so the
per-claim protections apply (graph-owned reviewer decisions, gated evidence requests, search budget,
auto-draft). Each sub-run hands back a summary built by code from its state, never written by the model.
The parent keeps the triage result verbatim plus those summaries; nothing it needs lives in old chat."""

from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from .audit import AuditLog, args_hash, default_log, execute_approved, make_event, source_ids
from .compact import tool_text
from .graph import (MAX_GATED_CALLS, _compact, _jsonish, build_graph, call_signature, check_position, describe_writes,
                    open_gaps)

# Model tokens (provider-reported input+output). Live case 3 (review 5) spent ~120k over 17 calls, ~7k a call,
# and never finished 0110. Each sub-run gets min(SUBRUN, what is left of BATCH), so the batch always reaches
# finish with a code-built summary for every claim, even if a claim run stops early on its budget.
SUBRUN_TOKEN_BUDGET = 35000
BATCH_TOKEN_BUDGET = 105000
APPROVE_WORDS = re.compile(r"\b(approve|approval|setujui)\b", re.I)
# Fixed, smaller tool set for every sub-run: the parent already ran triage and policy and owns the bulk
# approval, and a claim run never needs to list claims. Static per build, so the bound list never changes.
SUBRUN_DROP = frozenset({"triage_claims", "get_policy", "list_claims", "approve_cleared_batch"})
CLAIM_ID = re.compile(r"\bCLM-\d{4}-\d{4}\b")
RP = re.compile(r"Rp\s?(\d[\d.,]*\d|\d)")


def rp_figures(text: str) -> set[int]:
    out = set()
    for x in RP.findall(text or ""):
        digits = re.sub(r"[.,]", "", x)
        if digits.isdigit():
            out.add(int(digits))
    return out


def fair_share(remaining: int, queue: list[str], triage_text: str, cap: int | None) -> int:
    """This claim's slice of what is left of the batch budget: remaining split over the claims still queued,
    weighted by 1 + the number of open gaps triage reported for each, never above cap."""
    data = _jsonish(triage_text) or {}
    gaps = {r.get("claim_id"): len(r.get("gaps") or []) for r in data.get("exceptions") or []}
    w = [1 + gaps.get(c, 0) for c in queue] or [1]
    share = remaining * w[0] // sum(w)
    return min(cap, share) if cap is not None else share


def _merge(a, b):
    return {**(a or {}), **(b or {})}


class BatchState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    batch_id: str
    triage_text: str  # triage_claims output, verbatim
    policy_text: str  # get_policy output (compacted), fetched once and served to every sub-run
    tokens_used: int  # model tokens all sub-runs used so far
    cleared: list[str]
    queue: list[str]  # exception claims not yet investigated
    summaries: list[dict]  # one per finished sub-run, built by claim_summary()
    writes: list[dict]
    gated_count: int
    graph_proposals: list[dict]
    exec_queue: list[dict]  # approve's decision, waiting for approve_exec
    effective_args: Annotated[dict[str, dict], _merge]
    rp_unsupported: list[int]
    threads: Annotated[dict[str, dict], _merge]  # claim -> {"thread_id", "state"}: each sub-run's own state, kept
    # in the parent checkpoint so a later reviewer reply naming that claim continues that sub-run


def claim_summary(cid: str, v: dict) -> dict:
    """Code-built hand-back from a finished sub-run's state."""
    msgs = v.get("messages", [])
    draft = next((d["content"] for d in reversed(v.get("auto_drafts", []))), None)
    if draft is None:
        draft = next((str(m.content) for m in reversed(msgs)
                      if getattr(m, "type", "") == "tool" and m.name == "draft_decision"), None)
    pos = check_position(msgs, cid)
    eff = v.get("effective_args", {})
    used = list(eff.get(pos[1], {}).get("evidence_ids") or []) if pos else []
    return {"claim_id": cid, "draft": draft, "writes": list(v.get("writes", [])),
            "writes_text": describe_writes(v.get("writes", [])),
            "reviewer_decisions": dict(v.get("reviewer_decisions", {})),
            "evidence_used": used, "evidence_ignored": dict(v.get("ignored_ids", {})),
            "open_gaps": open_gaps(pos[2]) if pos else None}


def summary_text(s: dict) -> str:
    parts = [f"### {s['claim_id']}"]
    parts.append(f"Open gaps: {', '.join(s['open_gaps']) if s['open_gaps'] else 'none'}" if s["open_gaps"] is not None
                 else "Open gaps: not checked")
    if s["reviewer_decisions"]:
        parts.append(f"Reviewer decisions: {json.dumps(s['reviewer_decisions'])}")
    parts.append(f"Evidence used: {', '.join(s['evidence_used']) or 'none'}")
    if s["evidence_ignored"]:
        parts.append("Evidence ignored: " + ", ".join(f"{k} ({r})" for k, r in s["evidence_ignored"].items()))
    parts.append(s["writes_text"])
    parts.append(s["draft"] if s["draft"] else "No draft: the claim run stopped before a draft (needs human).")
    return "\n".join(parts)


def policy_summary(policy_text: str) -> str:
    """The numbers a claim review needs (tiers, caps, rates, required documents), without the rule prose:
    the engine applies the rules in check_claim, and the full policy is still served on get_policy."""
    p = _jsonish(policy_text) or {}
    keep = ("currency", "city_tiers", "default_tier", "hotel_nightly_cap", "per_diem_daily", "late_checkout",
            "receipt_required_categories", "boarding_pass_required_for")
    return json.dumps({k: p[k] for k in keep if k in p}, ensure_ascii=False, separators=(",", ":"))


def triage_row(triage_text: str, cid: str) -> str:
    data = _jsonish(triage_text) or {}
    row = next((r for r in data.get("exceptions") or [] if r.get("claim_id") == cid), None)
    return json.dumps(row, ensure_ascii=False) if row else ""


def build_batch_graph(model, tools, batch_id: str, system_prompt: str | None = None, checkpointer=None,
                      compact: bool = True, max_gated: int = MAX_GATED_CALLS,
                      subrun_token_budget: int | None = SUBRUN_TOKEN_BUDGET,
                      batch_token_budget: int | None = BATCH_TOKEN_BUDGET, slim: bool = True,
                      audit: AuditLog | None = None):
    """slim=False is the review-5 sub-run (bare prompt, nothing served), kept for the token comparison."""
    by_name = {t.name: t for t in tools}
    alog = audit if audit is not None else default_log()
    bnonce = uuid.uuid4().hex[:8]

    def run_id_of(config) -> str:
        return f"{bnonce}:{((config or {}).get('configurable') or {}).get('thread_id', 'default')}"

    async def call(name, args):
        res = await by_name[name].ainvoke(args)
        return res if isinstance(res, str) else tool_text(res)

    async def triage(state: BatchState):
        if state.get("triage_text"):  # a later reviewer turn: the batch run is already done
            return {}
        text = await call("triage_claims", {"batch": batch_id})
        data = _jsonish(text) or {}
        return {"batch_id": batch_id, "triage_text": text, "cleared": list(data.get("auto_cleared") or []),
                "queue": [r["claim_id"] for r in data.get("exceptions") or [] if r.get("claim_id")],
                "policy_text": _compact(await call("get_policy", {})) if "get_policy" in by_name else "",
                "summaries": [], "writes": [], "gated_count": 0, "graph_proposals": []}

    async def approve(state: BatchState, config):
        """Gate only: one interrupt, decision logged. approve_exec runs the approved batch approval."""
        first = next((m for m in state["messages"] if isinstance(m, HumanMessage)), None)
        quote = str(first.content) if first else ""
        cleared = state.get("cleared") or []
        if not cleared or not APPROVE_WORDS.search(quote) or "approve_cleared_batch" not in by_name:
            return {}
        args = {"batch_id": batch_id, "claim_ids": cleared, "reviewer_confirmed": True}
        tcid = "graph-approve_cleared_batch-0"
        rid = run_id_of(config)
        alog.append(make_event(rid, "staged_action", tcid, action_id=tcid, args_hash=args_hash("approve_cleared_batch", args),
                               payload={"tool": "approve_cleared_batch", "args": args, "by": "graph"},
                               sources=source_ids(args)))
        answer = interrupt({"tool": "approve_cleared_batch", "args": args, "tool_call_id": tcid, "by": "graph",
                            "from": quote, "prompt": f'Approve the {len(cleared)} claims triage cleared? You asked: "{quote}"'})
        ok = isinstance(answer, dict) and answer.get("approved") is True and answer.get("tool_call_id") == tcid
        reason = None if ok else ((answer.get("reason") if isinstance(answer, dict) else "") or "declined")
        try:
            alog.append(make_event(rid, "approval" if ok else "rejection", tcid, actor="human:reviewer", action_id=tcid,
                                   args_hash=args_hash("approve_cleared_batch", args),
                                   payload={"tool": "approve_cleared_batch", **({"reason": reason} if reason else {})}))
        except Exception:
            ok, reason = False, "approval could not be logged"
        return {"gated_count": state.get("gated_count", 0) + 1,
                "exec_queue": [{"tool": "approve_cleared_batch", "args": args, "id": tcid, "ok": ok,
                                "reason": reason, "quote": quote}]}

    async def approve_exec(state: BatchState, config):
        """Executor: runs the approved batch approval via execute_approved (one execution per action_id)."""
        writes = list(state.get("writes", []))
        props = list(state.get("graph_proposals", []))
        for q in state.get("exec_queue", []):
            args, tcid, ok = q["args"], q["id"], q["ok"]
            rec = {"tool": "approve_cleared_batch", "args": args, "id": tcid, "approved": ok, "from": q["quote"]}
            if not ok:
                rec["reason"] = q["reason"]
                props.append(rec)
                continue
            schema = getattr(by_name["approve_cleared_batch"], "args_schema", None)
            takes = "action_id" in (schema.get("properties", {}) if isinstance(schema, dict)
                                    else getattr(schema, "model_fields", {}) or {})
            r = await execute_approved(alog, run_id_of(config), tcid, "approve_cleared_batch", args,
                                       lambda a: call("approve_cleared_batch", {**a, "action_id": tcid} if takes else a))
            res = (_jsonish(r["result"]) or {}) if r["status"] == "executed" else {"error": r.get("reason") or r["status"]}
            rec["error"] = not res.get("ok")
            if res.get("ok"):
                writes.append({"tool": "approve_cleared_batch", "args": args, "by": "graph_action"})
            props.append(rec)
        return {"writes": writes, "graph_proposals": props, "exec_queue": []}

    sub_tools = [t for t in tools if t.name not in SUBRUN_DROP] if slim else tools

    def budget_for(state: BatchState, queue: list[str]) -> int | None:
        spent = state.get("tokens_used", 0)
        if batch_token_budget is None:
            return subrun_token_budget
        return fair_share(max(0, batch_token_budget - spent), queue, state.get("triage_text", ""), subrun_token_budget)

    def merged(state: BatchState, cid: str, v: dict, before: dict | None = None) -> dict:
        """Parent update from a finished sub-run state v. before: the claim's previous sub-run state (a
        reviewer reply continues it), so only what is new in v is added to the batch totals."""
        before = before or {}
        s = claim_summary(cid, v)
        new_writes = s["writes"][len(before.get("writes", [])):]
        summaries = [x for x in state.get("summaries", []) if x["claim_id"] != cid] + [s]
        return {"summaries": summaries, "writes": [*state.get("writes", []), *new_writes],
                "gated_count": state.get("gated_count", 0) + v.get("gated_count", 0) - before.get("gated_count", 0),
                "tokens_used": state.get("tokens_used", 0) + v.get("tokens_used", 0) - before.get("tokens_used", 0),
                "effective_args": v.get("effective_args", {}),
                "threads": {cid: {"thread_id": f"claim:{cid}", "state": v}}}

    async def claim_run(state: BatchState):
        """One exception claim: a fresh graph run (empty history, run_claim set) on its own per-claim state
        slot. Nothing before sub.ainvoke has a side effect: on resume after an interrupt this node re-runs
        from the top and the sub-run resumes from its checkpoint under the parent's checkpointer."""
        cid = state["queue"][0]
        left = max(0, max_gated - state.get("gated_count", 0))
        sub = build_graph(model, sub_tools, system_prompt=system_prompt, compact=compact, max_gated=left,
                          run_claim=cid, served=None, token_budget=budget_for(state, state["queue"]),
                          audit=alog, run_prefix=f"{bnonce}:{cid}")
        first = next((m for m in state["messages"] if isinstance(m, HumanMessage)), None)
        # Triage and policy are already known: hand them in (the sub-run's tool set has no triage/policy tools).
        prompt = (f"Review claim {cid} and draft the decision note. It is an exception in batch {batch_id} "
                  f"(the reviewer's request: \"{first.content if first else ''}\")."
                  + (f"\n\nTriage row for {cid} (already run by the batch):\n{triage_row(state.get('triage_text', ''), cid)}"
                     if slim else "")
                  + (f"\n\nPolicy summary (already loaded by the batch):\n{policy_summary(state['policy_text'])}"
                     if slim and state.get("policy_text") else ""))
        v = await sub.ainvoke({"messages": [HumanMessage(prompt)], "declined": {}})
        return {"queue": state["queue"][1:], **merged(state, cid, v)}

    def reply_target(state: BatchState) -> str | None:
        """The one sub-run claim a later reviewer message names, if exactly one."""
        last = state["messages"][-1] if state.get("messages") else None
        if not isinstance(last, HumanMessage) or len([m for m in state["messages"] if isinstance(m, HumanMessage)]) < 2:
            return None
        named = set(CLAIM_ID.findall(str(last.content))) & set(state.get("threads", {}))
        return named.pop() if len(named) == 1 else None

    async def claim_reply(state: BatchState):
        """Route a reviewer reply that names one claim to that claim's sub-run, continuing its own state."""
        cid = reply_target(state)
        prev = state["threads"][cid]["state"]
        left = max(0, max_gated - state.get("gated_count", 0))
        budget = budget_for(state, [cid])
        sub = build_graph(model, sub_tools, system_prompt=system_prompt, compact=compact, max_gated=left + prev.get("gated_count", 0),
                          run_claim=cid, served=None,
                          token_budget=None if budget is None else prev.get("tokens_used", 0) + budget,
                          audit=alog, run_prefix=f"{bnonce}:{cid}")
        v = await sub.ainvoke({**prev, "messages": [*prev.get("messages", []), state["messages"][-1]]})
        out = merged(state, cid, v, before=prev)
        s = next(x for x in out["summaries"] if x["claim_id"] == cid)
        text = summary_text(s)
        tail = next((str(m.content) for m in reversed(v.get("messages", [])) if isinstance(m, AIMessage) and not m.tool_calls), "")
        if tail and tail not in text:
            text = f"{tail}\n\n{text}"
        return {**out, "messages": [AIMessage(content=text)]}

    def finish(state: BatchState):
        if state["messages"] and isinstance(state["messages"][-1], AIMessage):
            return {}
        if len([m for m in state["messages"] if isinstance(m, HumanMessage)]) > 1:
            ids = ", ".join(state.get("threads", {})) or "none"
            return {"messages": [AIMessage(content=f"Batch {batch_id} is already processed in this run. Name one "
                                                    f"claim to continue its review (claim runs: {ids}).")]}
        props = state.get("graph_proposals", [])
        bulk = next((p for p in props if p["tool"] == "approve_cleared_batch"), None)
        head = ("Bulk approval: not proposed." if bulk is None else
                f"Bulk approval of {len(bulk['args']['claim_ids'])} cleared claims: "
                + ("done." if bulk["approved"] and not bulk.get("error") else
                   "failed." if bulk["approved"] else f"declined ({bulk.get('reason')})."))
        body = [f"## Batch {batch_id}", "Triage (verbatim):", state.get("triage_text", ""), head,
                *(summary_text(s) for s in state.get("summaries", [])), describe_writes(state.get("writes", []))]
        text = "\n\n".join(body)
        # Invented-Rp guard on the code-built text: every figure must come from triage, policy or an engine draft.
        known = rp_figures(state.get("triage_text", "")) | rp_figures(state.get("policy_text", ""))
        for x in state.get("summaries", []):
            known |= rp_figures(x.get("draft") or "")
        bad = sorted(rp_figures(text) - known)
        if bad:
            text += f"\n\nWarning: amounts not found in any engine output: {bad}. Check before using this summary."
        return {"messages": [AIMessage(content=text)], "rp_unsupported": bad}

    def next_step(state: BatchState):
        return "claim_run" if state.get("queue") else "finish"

    g = StateGraph(BatchState)
    g.add_node("triage", triage)
    g.add_node("approve", approve)
    g.add_node("approve_exec", approve_exec)
    g.add_edge("approve", "approve_exec")
    g.add_node("claim_run", claim_run)
    g.add_node("finish", finish)
    g.add_edge(START, "triage")
    g.add_node("claim_reply", claim_reply)
    g.add_conditional_edges("triage", lambda s: ("claim_reply" if reply_target(s) else "finish") if len(
        [m for m in s["messages"] if isinstance(m, HumanMessage)]) > 1 else "approve", ["approve", "finish", "claim_reply"])
    g.add_edge("claim_reply", END)
    g.add_conditional_edges("approve_exec", next_step, ["claim_run", "finish"])
    g.add_conditional_edges("claim_run", next_step, ["claim_run", "finish"])
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)


__all__ = ["build_batch_graph", "claim_summary", "summary_text", "call_signature"]
