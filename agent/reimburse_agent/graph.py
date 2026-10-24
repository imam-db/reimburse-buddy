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
    # fixed-template evidence ask, owner-only, idempotent (server builds the text; see
    # src/reimburse_buddy/evidence_request.py), so it needs no reviewer.
    "request_missing_evidence",
})
# Never offered to the agent at all: wipes outbox, statuses and replies.
HIDDEN_TOOLS = frozenset({"reset_demo"})

# send_message carries free text the model wrote, so it is always gated (it is not in
# READ_ONLY_TOOLS). Evidence requests go through request_missing_evidence instead.


def load_system_prompt(path: Path = SKILL_PATH) -> str:
    """SKILL.md as-is (YAML frontmatter included), the same text WorkBuddy loads."""
    return path.read_text(encoding="utf-8")


def needs_confirmation(name: str, args: dict[str, Any] | None = None) -> bool:
    """Deny by default: everything outside the allowlist, including every send_message."""
    return name not in READ_ONLY_TOOLS


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

    async def agent(state: State):
        return {"messages": [await bound.ainvoke([system, *state["messages"]])]}

    def gate(state: State):
        last = state["messages"][-1]
        declined: dict[str, str] = {}
        count = state.get("gated_count", 0)
        sigs = list(state.get("declined_sigs", []))
        for call in last.tool_calls:
            if call["name"] not in by_name:
                continue  # unknown or hidden tool: run_tools returns an error, no prompt for the reviewer
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
