"""Minimal tool-calling loop with a human gate before write/decision tools.

agent --(tool calls)--> gate --> tools --> agent ... --(no tool calls)--> END

The gate calls LangGraph `interrupt()` before any call that changes state or communicates a
decision, so the reviewer must answer before it runs. Resume with
`Command(resume={"approved": True, "tool_call_id": <id>})`; an answer without the matching id is a decline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from .compact import compact as _compact, tool_text

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "claim-auditor" / "SKILL.md"

# Allowlist: only these read-only tools run without the reviewer. Every other tool, including any
# write tool added to the server later, stops for approval (deny by default).
READ_ONLY_TOOLS = frozenset({
    "list_claims", "get_claim", "get_policy", "search_reimburse_mailbox", "get_calendar",
    "find_approval_memo", "check_claim", "triage_claims", "draft_decision", "get_outbox",
})
# Never offered to the agent at all: wipes outbox, statuses and replies.
HIDDEN_TOOLS = frozenset({"reset_demo"})

# send_message may skip approval only as a plain evidence request: to the claim's owner, tied to a
# claim, and with no money in it (no "Rp", no number of 4+ digits). Structural, not word matching.
# Permanent fix (follow-up): a server tool request_missing_evidence(claim_id, item_id, gap_code)
# that builds the text itself and can only address the claim owner.


def _digits_run(text: str) -> bool:
    # 4+ digits, also when written with thousands separators (2.400.000, 6,590)
    return any(len(re.sub(r"[.,]", "", m)) >= 4 for m in re.findall(r"\d[\d.,]*\d|\d", text))


def send_is_safe(args: dict[str, Any], owner: str | None) -> bool:
    claim_id = str(args.get("claim_id") or "").strip()
    to = str(args.get("to") or "").strip()
    text = f"{args.get('subject', '')} {args.get('body', '')}"
    if not claim_id or not owner or to != owner:
        return False
    return "rp" not in text.lower() and not _digits_run(text)


def load_system_prompt(path: Path = SKILL_PATH) -> str:
    """SKILL.md as-is (YAML frontmatter included), the same text WorkBuddy loads."""
    return path.read_text(encoding="utf-8")


def needs_confirmation(name: str, args: dict[str, Any], owner: str | None = None) -> bool:
    """owner: employee_id of args['claim_id'] (looked up by the graph); None = unknown -> gate."""
    if name in READ_ONLY_TOOLS:
        return False
    if name == "send_message":
        return not send_is_safe(args, owner)
    return True


MAX_GATED_CALLS = 10  # per run; past this, gated calls are declined without asking (approval fatigue)


def call_signature(call: dict) -> str:
    return json.dumps([call["name"], call.get("args", {})], sort_keys=True, default=str)


class State(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    # tool_call_id -> reason, for calls the reviewer declined
    declined: dict[str, str]
    gated_count: int  # interrupts shown so far in this run
    declined_sigs: list[str]  # (name, args) of calls the reviewer declined


def build_graph(model, tools, system_prompt: str | None = None, checkpointer=None, compact: bool = True,
                max_gated: int = MAX_GATED_CALLS):
    """model: any LangChain chat model with bind_tools. tools: LangChain tools (e.g. from MCP).
    compact: shrink tool outputs before the model sees them (see compact.py); False = raw."""
    system = SystemMessage(system_prompt if system_prompt is not None else load_system_prompt())
    tools = [t for t in tools if t.name not in HIDDEN_TOOLS]
    by_name = {t.name: t for t in tools}
    bound = model.bind_tools(tools)

    async def claim_owner(claim_id: str) -> str | None:
        if not claim_id or "get_claim" not in by_name:
            return None
        try:
            data = json.loads(tool_text(await by_name["get_claim"].ainvoke({"claim_id": claim_id})))
            return data.get("employee_id") if isinstance(data, dict) else None
        except Exception:
            return None

    async def agent(state: State):
        return {"messages": [await bound.ainvoke([system, *state["messages"]])]}

    async def gate(state: State):
        last = state["messages"][-1]
        declined: dict[str, str] = {}
        count = state.get("gated_count", 0)
        sigs = list(state.get("declined_sigs", []))
        for call in last.tool_calls:
            if call["name"] not in by_name:
                continue  # unknown or hidden tool: run_tools returns an error, no prompt for the reviewer
            owner = await claim_owner(str(call["args"].get("claim_id") or "")) if call["name"] == "send_message" else None
            if not needs_confirmation(call["name"], call["args"], owner):
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
        return {"declined": declined, "gated_count": count, "declined_sigs": sigs}

    async def run_tools(state: State):
        last = state["messages"][-1]
        out = []
        for call in last.tool_calls:
            if call["id"] in state.get("declined", {}):
                content = json.dumps({"error": "reviewer_declined", "reason": state["declined"][call["id"]]})
            elif call["name"] not in by_name:  # includes hidden tools like reset_demo
                content = json.dumps({"error": f"unknown tool {call['name']}"})
            else:
                try:
                    res = await by_name[call["name"]].ainvoke(call["args"])
                    if compact:
                        content = _compact(tool_text(res))
                    else:
                        content = res if isinstance(res, str) else json.dumps(res, default=str, ensure_ascii=False)
                except Exception as exc:  # surface tool errors to the model, don't crash the run
                    content = json.dumps({"error": str(exc)})
            out.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
        return {"messages": out, "declined": {}}

    def route(state: State):
        last = state["messages"][-1]
        return "gate" if isinstance(last, AIMessage) and last.tool_calls else END

    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_node("gate", gate)
    g.add_node("tools", run_tools)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, ["gate", END])
    g.add_edge("gate", "tools")
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)
