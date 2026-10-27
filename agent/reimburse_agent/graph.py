"""Minimal tool-calling loop with a human gate before write/decision tools.

agent --(tool calls)--> gate --> tools --> agent ... --(no tool calls)--> END

The gate calls LangGraph `interrupt()` before any call that changes state or communicates a
decision, so the reviewer must answer before it runs. Resume with
`Command(resume={"approved": True})` or `{"approved": False, "reason": "..."}`.
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

ALWAYS_CONFIRM = {"update_claim_status", "approve_cleared_batch", "request_approval"}
# send_message is allowed for "please send the missing receipt"; a message that communicates a
# decision needs the reviewer first (SKILL.md hard rule 4). Matched in English and Indonesian.
DECISION_WORDS = re.compile(
    r"\b(approv\w*|reject\w*|declin\w*|denied|decision|reimburse(d|ment) (of|amount)|"
    r"disetujui|setujui|ditolak|tolak|keputusan|dibayar|dicairkan)\b",
    re.IGNORECASE,
)


def load_system_prompt(path: Path = SKILL_PATH) -> str:
    """SKILL.md as-is (YAML frontmatter included), the same text WorkBuddy loads."""
    return path.read_text(encoding="utf-8")


def needs_confirmation(name: str, args: dict[str, Any]) -> bool:
    if name in ALWAYS_CONFIRM:
        return True
    if name == "send_message":
        text = f"{args.get('subject', '')} {args.get('body', '')}"
        return bool(DECISION_WORDS.search(text))
    return False


class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    # tool_call_id -> reason, for calls the reviewer declined
    declined: dict[str, str]


def build_graph(model, tools, system_prompt: str | None = None, checkpointer=None, compact: bool = True):
    """model: any LangChain chat model with bind_tools. tools: LangChain tools (e.g. from MCP).
    compact: shrink tool outputs before the model sees them (see compact.py); False = raw."""
    system = SystemMessage(system_prompt if system_prompt is not None else load_system_prompt())
    by_name = {t.name: t for t in tools}
    bound = model.bind_tools(tools)

    async def agent(state: State):
        return {"messages": [await bound.ainvoke([system, *state["messages"]])]}

    def gate(state: State):
        last = state["messages"][-1]
        declined: dict[str, str] = {}
        for call in last.tool_calls:
            if needs_confirmation(call["name"], call["args"]):
                answer = interrupt({"tool": call["name"], "args": call["args"], "tool_call_id": call["id"]})
                ok = answer.get("approved") if isinstance(answer, dict) else str(answer).lower() in {"y", "yes"}
                if not ok:
                    reason = (answer.get("reason") if isinstance(answer, dict) else "") or "no reason given"
                    declined[call["id"]] = reason
        return {"declined": declined}

    async def run_tools(state: State):
        last = state["messages"][-1]
        out = []
        for call in last.tool_calls:
            if call["id"] in state.get("declined", {}):
                content = json.dumps({"error": "reviewer_declined", "reason": state["declined"][call["id"]]})
            elif call["name"] not in by_name:
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
