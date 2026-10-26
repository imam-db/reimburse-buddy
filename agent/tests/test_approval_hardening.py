"""Approval hardening (fake model, no key): every write executor is gated, including the graph's own
evidence requests. Nothing runs until an interrupt is resumed with a matching approval."""

import uuid
from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent.graph import EVIDENCE_TOOL, build_graph
from reimburse_agent.mcp_client import mcp_tools

from test_graph_interrupt import Scripted, call

HARD = Path(__file__).parents[1] / "eval" / "fixtures" / "hard"
ENV = {"REIMBURSE_ENABLE_EVIDENCE_TOOL": "1", "REIMBURSE_ENABLE_WRITE_TOOLS": "1",
       "REIMBURSE_BUDDY_DATA": str(HARD)}
HERO = "CLM-2026-0110"
OWNER = "EMP-001"
CASE6 = "CLM-2026-0203"

WRITES = {
    "send_message": {"to": OWNER, "body": "Please send the IT-5 receipt.", "claim_id": HERO},
    "update_claim_status": {"claim_id": HERO, "status": "on_hold"},
    "request_approval": {"manager_id": "EMP-100", "claim_id": HERO, "item_id": "IT-5", "reason": "please review"},
    EVIDENCE_TOOL: {"claim_id": HERO, "item_id": "IT-5", "gap_code": "receipt_missing"},
}


def counted(tools):
    """Wrap every write executor so a test can see whether it was really invoked."""
    calls = {name: 0 for name in WRITES}

    def wrap(t):
        if t.name not in WRITES:
            return t

        async def run(**kw):
            calls[t.name] += 1
            return await t.ainvoke(kw)
        return StructuredTool.from_function(coroutine=run, name=t.name, description=t.description,
                                            args_schema=t.args_schema)

    return [wrap(t) for t in tools], calls


async def drive(tools, script, answer, counts):
    """Run a graph, recording each interrupt and the counter state at that moment."""
    g = build_graph(Scripted(messages=iter(script)), tools, checkpointer=InMemorySaver(), run_claim=HERO)
    conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
    payload = {"messages": [("user", "go")], "declined": {}}
    seen, before = [], []
    while True:
        await g.ainvoke(payload, conf)
        st = await g.aget_state(conf)
        if not st.interrupts:
            return seen, before, st
        req = st.interrupts[0].value
        seen.append(req)
        if answer is None:  # paused: return without resuming
            return seen, before, st
        before.append(dict(counts))
        payload = Command(resume=answer(req))


async def test_no_write_executor_runs_while_paused():
    script = [AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": f"w{i}"} for i, (name, args) in enumerate(WRITES.items(), 1)])]
    async with mcp_tools() as (_s, tools):
        wrapped, calls = counted(tools)
        seen, _before, st = await drive(wrapped, script, answer=None, counts=calls)
    assert [r["tool"] for r in seen] == ["send_message"]  # paused at the first write
    assert calls == {name: 0 for name in WRITES} and st.interrupts


async def test_mismatched_or_malformed_approval_executes_nothing():
    """An approval that names another call (a replayed or edited token), or is not a dict, is a decline."""
    bad_answers = [lambda r: {"approved": True, "tool_call_id": "someone-else"},
                   lambda r: {"approved": "yes", "tool_call_id": r["tool_call_id"]},
                   lambda r: True]
    for answer in bad_answers:
        script = [call("send_message", WRITES["send_message"], 1), AIMessage(content="not done")]
        async with mcp_tools() as (_s, tools):
            wrapped, calls = counted(tools)
            seen, _before, st = await drive(wrapped, script, answer=answer, counts=calls)
        assert [r["tool"] for r in seen] == ["send_message"]
        assert calls == {n: 0 for n in WRITES}


async def test_every_approved_write_is_invoked_exactly_once():
    script = [AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": f"w{i}"} for i, (name, args) in enumerate(WRITES.items(), 1)]),
        AIMessage(content="done")]
    async with mcp_tools() as (_s, tools):
        wrapped, calls = counted(tools)
        seen, before, _st = await drive(
            wrapped, script, answer=lambda r: {"approved": True, "tool_call_id": r["tool_call_id"]}, counts=calls)
    assert [r["tool"] for r in seen] == list(WRITES)
    assert all(b == {name: 0 for name in WRITES} for b in before)  # nothing ran before any resume
    assert calls == {name: 1 for name in WRITES}


async def test_rejection_executes_nothing():
    for i, (name, args) in enumerate(WRITES.items(), 1):
        script = [call(name, args, i), AIMessage(content="not done")]
        async with mcp_tools() as (_s, tools):
            wrapped, calls = counted(tools)
            seen, _before, st = await drive(
                wrapped, script, counts=calls,
                answer=lambda r: {"approved": False, "reason": "no", "tool_call_id": r["tool_call_id"]})
        assert [r["tool"] for r in seen] == [name]
        assert calls == {n: 0 for n in WRITES}, name
        assert "reviewer_declined" in st.values["messages"][-2].content, name


async def test_graph_initiated_evidence_request_interrupts_before_running():
    q = lambda i: call("search_for_gap", {"item_id": "IT-2", "gap_code": "boarding_pass_missing",
                                          "query": f"pass {i}"}, 10 + i)
    script = [call("check_claim", {"claim_id": CASE6}, 1), q(1), q(2), q(3), q(4), q(5),
              AIMessage(content="done")]
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        g = build_graph(Scripted(messages=iter(script)), tools, checkpointer=InMemorySaver(), run_claim=CASE6)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
        payload = {"messages": [("user", "go")], "declined": {}}
        seen, before_request = [], []
        while True:
            await g.ainvoke(payload, conf)
            st = await g.aget_state(conf)
            if not st.interrupts:
                break
            req = st.interrupts[0].value
            seen.append(req)
            if req["tool"] == EVIDENCE_TOOL:
                out = await g.aget_state(conf)
                before_request.append([w for w in out.values.get("writes", [])])
            payload = Command(resume={"approved": True, "tool_call_id": req["tool_call_id"]})
    req = seen[0]
    assert req["tool"] == EVIDENCE_TOOL and req["by"] == "graph"
    assert req["args"] == {"claim_id": CASE6, "item_id": "IT-2", "gap_code": "boarding_pass_missing"}
    assert before_request == [[]]  # nothing written when the graph asked for approval
    assert [w["tool"] for w in st.values["writes"]] == [EVIDENCE_TOOL]


async def test_graph_initiated_evidence_request_rejected_sends_nothing():
    q = lambda i: call("search_for_gap", {"item_id": "IT-2", "gap_code": "boarding_pass_missing",
                                          "query": f"pass {i}"}, 10 + i)
    script = [call("check_claim", {"claim_id": CASE6}, 1), q(1), q(2), q(3), q(4), q(5),
              AIMessage(content="done")]
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        wrapped, calls = counted(tools)
        g = build_graph(Scripted(messages=iter(script)), wrapped, checkpointer=InMemorySaver(), run_claim=CASE6)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
        payload = {"messages": [("user", "go")], "declined": {}}
        seen = []
        while True:
            await g.ainvoke(payload, conf)
            st = await g.aget_state(conf)
            if not st.interrupts:
                break
            req = st.interrupts[0].value
            seen.append(req)
            payload = Command(resume={"approved": False, "reason": "not now", "tool_call_id": req["tool_call_id"]})
    assert [r["tool"] for r in seen] == [EVIDENCE_TOOL] and seen[0]["by"] == "graph"
    assert calls[EVIDENCE_TOOL] == 0 and not st.values.get("writes")
    assert any("declined" in str(m.content) for m in st.values["messages"] if getattr(m, "type", "") == "tool")
