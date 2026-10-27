"""Scripted chat model (no LLM, no key): prove the gate interrupts before write tools."""

import json
import uuid

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent.config import MissingConfig, load_config
from reimburse_agent.graph import build_graph, load_system_prompt, needs_confirmation
from reimburse_agent.mcp_client import mcp_tools


class Scripted(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


def call(name, args, i):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{i}"}])


def outbox(res):
    if isinstance(res, tuple):
        res = res[0]
    if isinstance(res, list):
        res = res[0]["text"] if isinstance(res[0], dict) else res[0]
    return json.loads(res) if isinstance(res, str) else res


async def _run(script, resume):
    async with mcp_tools() as (_s, tools):
        g = build_graph(Scripted(messages=iter(script)), tools, checkpointer=InMemorySaver())
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}}
        await g.ainvoke({"messages": [("user", "go")], "declined": {}}, conf)
        st = await g.aget_state(conf)
        by = {t.name: t for t in tools}
        before = outbox(await by["get_outbox"].ainvoke({}))
        assert st.interrupts, "expected an interrupt"
        intr = st.interrupts[0].value
        await g.ainvoke(Command(resume=resume), conf)
        after = outbox(await by["get_outbox"].ainvoke({}))
        st2 = await g.aget_state(conf)
        return intr, before, after, st2


@pytest.mark.parametrize("tool,args,key", [
    ("update_claim_status", {"claim_id": "CLM-2026-0110", "status": "approved"}, "status_changes"),
    ("request_approval", {"manager_id": "EMP-100", "claim_id": "CLM-2026-0110", "item_id": "IT-1"}, "approval_requests"),
])
async def test_interrupt_before_write_then_runs_on_approve(tool, args, key):
    script = [call(tool, args, 1), AIMessage(content="done")]
    intr, before, after, st = await _run(script, {"approved": True})
    assert intr["tool"] == tool and intr["args"] == args
    assert before[key] == []  # nothing written while paused
    assert len(after[key]) == 1  # the write happened only after approval
    assert st.values["messages"][-1].content == "done"


async def test_declined_write_never_reaches_server():
    script = [call("update_claim_status", {"claim_id": "CLM-2026-0110", "status": "rejected"}, 1),
              AIMessage(content="ok, not changed")]
    intr, before, after, st = await _run(script, {"approved": False, "reason": "no"})
    assert before == after and after["status_changes"] == []
    tool_msg = st.values["messages"][-2]
    assert "reviewer_declined" in tool_msg.content


async def test_approve_cleared_batch_is_gated():
    script = [call("approve_cleared_batch", {"batch_id": "2026-W43", "claim_ids": [], "reviewer_confirmed": True}, 1),
              AIMessage(content="x")]
    intr, *_ = await _run(script, {"approved": False})
    assert intr["tool"] == "approve_cleared_batch"


async def test_read_tools_and_document_requests_do_not_interrupt():
    script = [call("check_claim", {"claim_id": "CLM-2026-0110"}, 1),
              call("send_message", {"to": "EMP-1", "body": "Please forward the missing hotel receipt."}, 2),
              AIMessage(content="asked for receipt")]
    async with mcp_tools() as (_s, tools):
        g = build_graph(Scripted(messages=iter(script)), tools, checkpointer=InMemorySaver())
        conf = {"configurable": {"thread_id": "t"}}
        await g.ainvoke({"messages": [("user", "go")], "declined": {}}, conf)
        st = await g.aget_state(conf)
        assert not st.interrupts
        assert "4840000" in st.values["messages"][2].content


def test_decision_messages_are_gated():
    assert needs_confirmation("send_message", {"body": "Your claim was approved"})
    assert needs_confirmation("send_message", {"body": "Klaim Anda ditolak"})
    assert not needs_confirmation("send_message", {"body": "Mohon kirim struk hotel"})
    assert not needs_confirmation("check_claim", {})


def test_system_prompt_is_skill_md_verbatim():
    p = load_system_prompt()
    assert p.startswith("---\nname: claim-auditor")
    assert "Never compute money yourself" in p


def test_config_requires_env_and_hides_key():
    with pytest.raises(MissingConfig):
        load_config({})
    cfg = load_config({"REIMBURSE_AGENT_MODEL": "m", "REIMBURSE_AGENT_API_KEY": "sk-secret"})
    assert "sk-secret" not in repr(cfg)
