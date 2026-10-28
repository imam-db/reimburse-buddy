"""One reviewer instruction -> one approval prompt (fake model, no key). The graph used to queue the same
instruction twice (intake) and propose identical calls twice in one pass (graph_actions)."""

import uuid

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent import audit as A
from reimburse_agent.graph import build_graph
from reimburse_agent.mcp_client import mcp_tools

from test_approval_hardening import ENV, counted
from test_graph_interrupt import Scripted, call

CID = "CLM-2026-0111"  # clean claim: every instruction is fully determined
NOTICE = f"Review claim {CID}. Tell the employee the outcome and the amount."
STATUS = "Then set the claim status to record your decision."
PENDING_NOTICE = {"kind": "outcome_notice", "quote": "Tell the employee the outcome and the amount.", "at": 0}


def _script(n=6):
    return [call("check_claim", {"claim_id": CID}, 1)] + [AIMessage(content="done")] * n


async def _drive(g, conf, payload, approve, seen):
    while True:
        await g.ainvoke(payload, conf)
        st = await g.aget_state(conf)
        if not st.interrupts:
            return st
        req = st.interrupts[0].value
        seen.append(req)
        payload = Command(resume={"approved": approve, "reason": "no", "tool_call_id": req["tool_call_id"]})


def _graph(tools, log):
    return build_graph(Scripted(messages=iter(_script())), tools, checkpointer=InMemorySaver(),
                       run_claim=CID, audit=log)


def _conf():
    return {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 80}


def _staged(log):
    return [e for e in log.list() if e["event_type"] == "staged_action"]


async def test_one_reviewer_push_is_one_prompt_and_one_staged_action():
    """The same instruction already queued (and re-asked in the new turn) is proposed once."""
    log = A.JsonlAuditLog()
    seen = []
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        wrapped, calls = counted(tools)
        g = _graph(wrapped, log)
        payload = {"messages": [("user", NOTICE)], "declined": {}, "instructions": [dict(PENDING_NOTICE)]}
        st = await _drive(g, _conf(), payload, True, seen)
    assert [r["tool"] for r in seen] == ["send_message"], seen
    assert len(_staged(log)) == 1
    assert calls["send_message"] == 1
    assert [w["tool"] for w in st.values["writes"]] == ["send_message"]


async def test_identical_calls_from_two_worded_instructions_are_proposed_once():
    """Different quotes, same call signature: graph_actions asks once."""
    log = A.JsonlAuditLog()
    seen = []
    other = {"kind": "outcome_notice", "quote": "Inform the claimant of the decision amount.", "at": 0}
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        g = _graph(tools, log)
        payload = {"messages": [("user", NOTICE)], "declined": {}, "instructions": [other]}
        await _drive(g, _conf(), payload, True, seen)
    assert [r["tool"] for r in seen] == ["send_message"], seen
    assert len(_staged(log)) == 1


async def test_two_different_instructions_in_one_turn_are_two_proposals():
    log = A.JsonlAuditLog()
    seen = []
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        g = _graph(tools, log)
        await _drive(g, _conf(), {"messages": [("user", f"{NOTICE} {STATUS}")], "declined": {}}, True, seen)
    assert [r["tool"] for r in seen] == ["send_message", "update_claim_status"]
    assert len(_staged(log)) == 2


async def test_declined_instruction_repeated_later_is_not_proposed_again():
    log = A.JsonlAuditLog()
    seen = []
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        wrapped, calls = counted(tools)
        g = _graph(wrapped, log)
        conf = _conf()
        await _drive(g, conf, {"messages": [("user", NOTICE)], "declined": {}}, False, seen)
        assert [r["tool"] for r in seen] == ["send_message"]
        await _drive(g, conf, {"messages": [("user", "Please tell the employee the outcome and the amount.")]},
                     False, seen)
    assert [r["tool"] for r in seen] == ["send_message"], seen  # not asked a second time
    assert calls["send_message"] == 0
    assert len(_staged(log)) == 1
