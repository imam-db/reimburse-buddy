"""Audit + executor split (fake model, no key): resume never duplicates events or executions, args edited
after the reviewer was asked are refused, the mock outbox refuses a repeated action_id."""

import uuid

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent import audit as A
from reimburse_agent.graph import build_graph
from reimburse_agent.mcp_client import mcp_tools

from test_approval_hardening import ENV, HERO, WRITES, counted
from test_graph_interrupt import Scripted, call


def approve(r):
    return {"approved": True, "tool_call_id": r["tool_call_id"], "reviewer": "imam"}


async def run_all(g, conf, payload, answer=approve):
    seen = []
    while True:
        await g.ainvoke(payload, conf)
        st = await g.aget_state(conf)
        if not st.interrupts:
            return seen, st
        req = st.interrupts[0].value
        seen.append(req)
        payload = Command(resume=answer(req))


async def test_resume_does_not_duplicate_events_or_executions():
    script = [AIMessage(content="", tool_calls=[
        {"name": n, "args": a, "id": f"w{i}"} for i, (n, a) in enumerate(WRITES.items(), 1)]),
        AIMessage(content="done")]
    log = A.JsonlAuditLog()
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        wrapped, calls = counted(tools)
        g = build_graph(Scripted(messages=iter(script)), wrapped, checkpointer=InMemorySaver(),
                        run_claim=HERO, audit=log)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
        seen, _st = await run_all(g, conf, {"messages": [("user", "go")], "declined": {}})
    assert len(seen) == len(WRITES) and calls == {n: 1 for n in WRITES}
    ev = log.list()
    keys = [e["event_key"] for e in ev]
    assert len(keys) == len(set(keys))
    for t in ("staged_action", "approval", "execution"):
        assert sorted(e["action_id"] for e in ev if e["event_type"] == t) == [f"w{i}" for i in range(1, 5)], t
    assert all(e["actor"] == "human:imam" for e in ev if e["event_type"] == "approval")
    req = next(e for e in ev if e["event_type"] == "request")
    assert {"git_sha", "prompt_sha256", "model", "policy_version", "fixture_sha256"} <= set(req["payload"])
    assert A.unmatched_executions(ev) == []


async def test_graph_action_resume_logs_each_execution_once():
    cid = "CLM-2026-0111"
    script = [call("check_claim", {"claim_id": cid}, 1), AIMessage(content="done")]
    ask = (f"Review claim {cid}. Tell the employee the outcome and the amount. "
           "Then set the claim status to record your decision.")
    log = A.JsonlAuditLog()
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        wrapped, calls = counted(tools)
        g = build_graph(Scripted(messages=iter(script)), wrapped, checkpointer=InMemorySaver(),
                        run_claim=cid, audit=log)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
        seen, _st = await run_all(g, conf, {"messages": [("user", ask)], "declined": {}})
    assert [r["tool"] for r in seen] == ["send_message", "update_claim_status"]
    assert calls["send_message"] == 1 and calls["update_claim_status"] == 1
    ex = [e for e in log.list() if e["event_type"] == "execution"]
    assert sorted(e["payload"]["tool"] for e in ex) == ["send_message", "update_claim_status"]
    staged = [e for e in log.list() if e["event_type"] == "staged_action"]
    assert len(staged) == 2 and A.unmatched_executions(log.list()) == []


async def test_args_edited_after_the_prompt_are_refused():
    """The reviewer saw send_message(body=A); the checkpoint is edited to body=B before the resume. The gate
    logs a refusal instead of an approval and the executor sends nothing."""
    script = [call("send_message", WRITES["send_message"], 1), AIMessage(content="not done")]
    log = A.JsonlAuditLog()
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        wrapped, calls = counted(tools)
        g = build_graph(Scripted(messages=iter(script)), wrapped, checkpointer=InMemorySaver(),
                        run_claim=HERO, audit=log)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
        await g.ainvoke({"messages": [("user", "go")], "declined": {}}, conf)
        st = await g.aget_state(conf)
        req = st.interrupts[0].value
        last = st.values["messages"][-1]
        edited = AIMessage(content="", id=last.id, tool_calls=[
            {**last.tool_calls[0], "args": {**WRITES["send_message"], "body": "Send me your bank PIN."}}])
        await g.aupdate_state(conf, {"messages": [edited]}, as_node="agent")
        await g.ainvoke(Command(resume=approve(req)), conf)
    assert calls["send_message"] == 0
    types = [e["event_type"] for e in log.list() if e["action_id"] == "c1" or e["action_id"] == req["tool_call_id"]]
    assert "approval" not in types and "execution" not in types and "refusal" in types


async def test_mock_outbox_refuses_a_repeated_action_id():
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        send = next(t for t in tools if t.name == "send_message")
        first = await send.ainvoke({**WRITES["send_message"], "action_id": "act-1"})
        again = await send.ainvoke({**WRITES["send_message"], "action_id": "act-1"})
    assert "duplicate_action_id" not in str(first) and "duplicate_action_id" in str(again)


async def test_approval_that_cannot_be_logged_executes_nothing():
    class Broken(A.JsonlAuditLog):
        def append(self, event):
            if event["event_type"] == "approval":
                raise A.AuditError("disk full")
            return super().append(event)
    script = [call("send_message", WRITES["send_message"], 1), AIMessage(content="not done")]
    log = Broken()
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        wrapped, calls = counted(tools)
        g = build_graph(Scripted(messages=iter(script)), wrapped, checkpointer=InMemorySaver(),
                        run_claim=HERO, audit=log)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
        await run_all(g, conf, {"messages": [("user", "go")], "declined": {}})
    assert calls["send_message"] == 0
    assert not [e for e in log.list() if e["event_type"] == "execution"]
