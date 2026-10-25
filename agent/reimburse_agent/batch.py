"""Batch mode (version B): one parent run per batch, one fresh sub-run per exception claim.

The parent does triage and the reviewer-gated bulk approval itself (the reviewer's request is the
instruction; the arguments come from triage, so they are fully determined). Each exception claim then
gets its own graph run with run_claim set and an empty message history, on the same MCP session, so the
per-claim protections apply (graph-owned reviewer decisions, free evidence requests, search budget,
auto-draft). Each sub-run hands back a summary built by code from its state, never written by the model.
The parent keeps the triage result verbatim plus those summaries; nothing it needs lives in old chat."""

from __future__ import annotations

import json
import re
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from .compact import tool_text
from .graph import (MAX_GATED_CALLS, _compact, _jsonish, build_graph, call_signature, check_position, describe_writes,
                    open_gaps)

# Model tokens (provider-reported input+output). Live case 3 (review 5) spent ~120k over 17 calls, ~7k a call,
# and never finished 0110. Each sub-run gets min(SUBRUN, what is left of BATCH), so the batch always reaches
# finish with a code-built summary for every claim, even if a claim run stops early on its budget.
SUBRUN_TOKEN_BUDGET = 35000
BATCH_TOKEN_BUDGET = 105000
APPROVE_WORDS = re.compile(r"\b(approve|approval|setujui)\b", re.I)


def _merge(a, b):
    return {**(a or {}), **(b or {})}


class BatchState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    batch_id: str
    triage_text: str  # triage_claims output, verbatim
    policy_text: str  # get_policy output (compacted), fetched once and served to every sub-run
    free_evidence: list[list[str]]  # [claim, item, gap] free evidence requests the sub-runs sent
    tokens_used: int  # model tokens all sub-runs used so far
    cleared: list[str]
    queue: list[str]  # exception claims not yet investigated
    summaries: list[dict]  # one per finished sub-run, built by claim_summary()
    writes: list[dict]
    gated_count: int
    graph_proposals: list[dict]
    effective_args: Annotated[dict[str, dict], _merge]


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
    last_ai = next((str(m.content) for m in reversed(msgs) if getattr(m, "type", "") == "ai" and not m.tool_calls), "")
    return {"claim_id": cid, "draft": draft, "writes": list(v.get("writes", [])),
            "writes_text": describe_writes(v.get("writes", [])),
            "reviewer_decisions": dict(v.get("reviewer_decisions", {})),
            "evidence_used": used, "evidence_ignored": dict(v.get("ignored_ids", {})),
            "open_gaps": open_gaps(pos[2]) if pos else None,
            "stopped_with": None if draft else last_ai[:1500]}


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
    parts.append(s["draft"] if s["draft"] else f"No draft yet. The claim run stopped with: {s['stopped_with']}")
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
                      batch_token_budget: int | None = BATCH_TOKEN_BUDGET, slim: bool = True):
    """slim=False is the review-5 sub-run (bare prompt, nothing served), kept for the token comparison."""
    by_name = {t.name: t for t in tools}

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
                "summaries": [], "writes": [], "gated_count": 0, "graph_proposals": [], "free_evidence": []}

    async def approve(state: BatchState):
        first = next((m for m in state["messages"] if isinstance(m, HumanMessage)), None)
        quote = str(first.content) if first else ""
        cleared = state.get("cleared") or []
        if not cleared or not APPROVE_WORDS.search(quote) or "approve_cleared_batch" not in by_name:
            return {}
        args = {"batch_id": batch_id, "claim_ids": cleared, "reviewer_confirmed": True}
        tcid = "graph-approve_cleared_batch-0"
        answer = interrupt({"tool": "approve_cleared_batch", "args": args, "tool_call_id": tcid, "by": "graph",
                            "from": quote, "prompt": f'Approve the {len(cleared)} claims triage cleared? You asked: "{quote}"'})
        ok = isinstance(answer, dict) and answer.get("approved") is True and answer.get("tool_call_id") == tcid
        rec = {"tool": "approve_cleared_batch", "args": args, "id": tcid, "approved": ok, "from": quote}
        writes = list(state.get("writes", []))
        if ok:
            res = _jsonish(await call("approve_cleared_batch", args)) or {}
            rec["error"] = not res.get("ok")
            if res.get("ok"):
                writes.append({"tool": "approve_cleared_batch", "args": args, "by": "graph_action"})
        else:
            rec["reason"] = (answer.get("reason") if isinstance(answer, dict) else "") or "declined"
        return {"writes": writes, "gated_count": state.get("gated_count", 0) + 1,
                "graph_proposals": [*state.get("graph_proposals", []), rec]}

    async def claim_run(state: BatchState):
        """One exception claim: a fresh graph run (empty history, run_claim set). The sub-graph has no
        checkpointer of its own, so it is scoped to this call and its interrupts surface in the parent."""
        cid = state["queue"][0]
        left = max(0, max_gated - state.get("gated_count", 0))
        spent = state.get("tokens_used", 0)
        caps = [b for b in (subrun_token_budget, None if batch_token_budget is None else max(0, batch_token_budget - spent))
                if b is not None]
        budget = min(caps) if caps else None
        served = None if not slim else {"triage_claims": ({"batch": batch_id}, state.get("triage_text", ""))}
        if slim and state.get("policy_text"):
            served["get_policy"] = ({}, state["policy_text"])
        sub = build_graph(model, tools, system_prompt=system_prompt, compact=compact, max_gated=left, run_claim=cid,
                          served=served, token_budget=budget)
        first = next((m for m in state["messages"] if isinstance(m, HumanMessage)), None)
        # Triage and policy are already known: hand them in so the model need not call for them again
        # (if it does, the sub-run serves the held text; the tool list is unchanged).
        prompt = (f"Review claim {cid} and draft the decision note. It is an exception in batch {batch_id} "
                  f"(the reviewer's request: \"{first.content if first else ''}\")."
                  + (f"\n\nTriage row for {cid} (already run, do not call triage_claims again):\n{triage_row(state.get('triage_text', ''), cid)}"
                     if slim else "")
                  + (f"\n\nPolicy summary (already loaded, do not call get_policy again):\n{policy_summary(state['policy_text'])}"
                     if slim and state.get("policy_text") else ""))
        v = await sub.ainvoke({"messages": [HumanMessage(prompt)], "declined": {}})
        s = claim_summary(cid, v)
        return {"queue": state["queue"][1:], "summaries": [*state.get("summaries", []), s],
                "writes": [*state.get("writes", []), *s["writes"]],
                "gated_count": state.get("gated_count", 0) + v.get("gated_count", 0),
                "free_evidence": [*state.get("free_evidence", []), *v.get("free_evidence", [])],
                "tokens_used": spent + v.get("tokens_used", 0),
                "effective_args": v.get("effective_args", {})}

    def finish(state: BatchState):
        if state["messages"] and isinstance(state["messages"][-1], AIMessage):
            return {}
        if len([m for m in state["messages"] if isinstance(m, HumanMessage)]) > 1:
            return {"messages": [AIMessage(content=f"Batch {batch_id} is already processed in this run; "
                                                    "start a claim run to continue on one claim.")]}
        props = state.get("graph_proposals", [])
        bulk = next((p for p in props if p["tool"] == "approve_cleared_batch"), None)
        head = ("Bulk approval: not proposed." if bulk is None else
                f"Bulk approval of {len(bulk['args']['claim_ids'])} cleared claims: "
                + ("done." if bulk["approved"] and not bulk.get("error") else
                   "failed." if bulk["approved"] else f"declined ({bulk.get('reason')})."))
        body = [f"## Batch {batch_id}", "Triage (verbatim):", state.get("triage_text", ""), head,
                *(summary_text(s) for s in state.get("summaries", [])), describe_writes(state.get("writes", []))]
        return {"messages": [AIMessage(content="\n\n".join(body))]}

    def next_step(state: BatchState):
        return "claim_run" if state.get("queue") else "finish"

    g = StateGraph(BatchState)
    g.add_node("triage", triage)
    g.add_node("approve", approve)
    g.add_node("claim_run", claim_run)
    g.add_node("finish", finish)
    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", lambda s: "finish" if len(
        [m for m in s["messages"] if isinstance(m, HumanMessage)]) > 1 else "approve", ["approve", "finish"])
    g.add_conditional_edges("approve", next_step, ["claim_run", "finish"])
    g.add_conditional_edges("claim_run", next_step, ["claim_run", "finish"])
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)


__all__ = ["build_batch_graph", "claim_summary", "summary_text", "call_signature"]
