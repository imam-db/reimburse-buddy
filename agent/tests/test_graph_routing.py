"""Fake-script tests (no LLM, no key) for the graph's routing: clean check -> draft, mailbox search cache,
and exhausted gaps -> draft with the open items."""

import json

import pytest
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from reimburse_agent.graph import ALREADY_SEARCHED, build_graph, load_system_prompt
from reimburse_agent.mcp_client import mcp_tools

from test_graph_interrupt import Scripted, call

HARD = Path(__file__).parents[1] / "eval" / "fixtures" / "hard"
ENV = {"REIMBURSE_ENABLE_EVIDENCE_TOOL": "1"}


async def _run(script, run_claim, env=None):
    async with mcp_tools(extra_env={**ENV, **(env or {})}) as (_s, tools):
        g = build_graph(Scripted(messages=iter(script)), tools, checkpointer=InMemorySaver(), run_claim=run_claim)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}}
        await g.ainvoke({"messages": [("user", "go")], "declined": {}}, conf)
        st = await g.aget_state(conf)
        assert not st.interrupts
        return st.values["messages"]


def tools_called(msgs):
    return [m.name for m in msgs if m.type == "tool"]


async def test_clean_check_drafts_and_ends_without_another_model_call():
    cid = "CLM-2026-0111"
    msgs = await _run([call("check_claim", {"claim_id": cid}, 1)], cid)  # a 2nd model turn would raise
    assert tools_called(msgs) == ["check_claim"]  # no synthetic tool-call message in the history
    assert isinstance(msgs[-1], AIMessage) and not msgs[-1].tool_calls
    assert "no open gaps" in msgs[-1].content and "Rp" in msgs[-1].content and cid in msgs[-1].content


async def test_open_gap_does_not_auto_draft():
    cid = "CLM-2026-0110"
    msgs = await _run([call("check_claim", {"claim_id": cid}, 1), AIMessage(content="final")], cid)
    assert tools_called(msgs) == ["check_claim"]


async def test_repeated_search_returns_already_searched_until_a_message_goes_out():
    cid, emp = "CLM-2026-0110", "EMP-001"
    script = [call("check_claim", {"claim_id": cid}, 1),
              call("search_reimburse_mailbox", {"employee_id": emp, "query": "taxi receipt"}, 2),
              call("search_reimburse_mailbox", {"employee_id": emp, "query": "Receipt, TAXI"}, 3),
              call("request_missing_evidence", {"claim_id": cid, "item_id": "IT-5", "gap_code": "receipt_missing"}, 4),
              call("search_reimburse_mailbox", {"employee_id": emp, "query": "taxi receipt"}, 5),
              AIMessage(content="final")]
    msgs = await _run(script, cid)
    searches = [m.content for m in msgs if m.type == "tool" and m.name == "search_reimburse_mailbox"]
    assert ALREADY_SEARCHED not in searches[0] and ALREADY_SEARCHED in searches[1]
    assert ALREADY_SEARCHED not in searches[2] and "MSG-5203" in searches[2]  # the reply arrived after the ask


async def test_exhausted_gap_routes_to_draft_with_open_items():
    cid = "CLM-2026-0203"  # return boarding pass missing; nobody replies
    first = call("check_claim", {"claim_id": cid}, 1)
    script = [first, call("search_reimburse_mailbox", {"employee_id": "EMP-005", "query": "boarding pass"}, 2)]
    async with mcp_tools(extra_env={**ENV, "REIMBURSE_BUDDY_DATA": str(HARD)}) as (_s, tools):
        by = {t.name: t for t in tools}
        chk = json.loads((await by["check_claim"].ainvoke({"claim_id": cid}))[0]["text"])
    item, gap = next((i["item_id"], g) for i in chk["items"] for g in i.get("gaps", []))
    script += [call("request_missing_evidence", {"claim_id": cid, "item_id": item, "gap_code": gap}, 3),
               call("check_claim", {"claim_id": cid}, 4), AIMessage(content="final")]
    msgs = await _run(script[:-1], cid, {"REIMBURSE_BUDDY_DATA": str(HARD)})
    assert tools_called(msgs)[-1] == "check_claim" and not msgs[-1].tool_calls
    assert f"{item}:{gap}" in msgs[-1].content and "still open" in msgs[-1].content


def test_system_prompt_is_byte_identical_across_builds():
    a, b = load_system_prompt(), load_system_prompt()
    assert a == b and a.encode() == b.encode()
    assert "at most once per gap" not in a and "Never pick one receipt yourself" in a


# --- DeepSeek thinking-mode replay -------------------------------------------------------------------
class ThinkingProvider(Scripted):
    """Simulates DeepSeek thinking mode's request validation: every assistant message with tool calls
    in the history must carry the reasoning_content the provider generated. Its own replies carry it."""

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        for m in messages:
            if isinstance(m, AIMessage) and m.tool_calls and "reasoning_content" not in m.additional_kwargs:
                raise ValueError("400: The `reasoning_content` in the thinking mode must be passed back to the API.")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)


def thought(name, args, i):
    m = call(name, args, i)
    m.additional_kwargs["reasoning_content"] = "..."
    return m


def test_provider_simulation_rejects_the_old_inserted_message():
    old = AIMessage(content="", tool_calls=[{"name": "draft_decision", "args": {}, "id": "auto-draft-c2"}])
    with pytest.raises(ValueError, match="reasoning_content"):
        ThinkingProvider(messages=iter([AIMessage(content="x")])).invoke([old])


async def test_smoke_case8_history_with_reviewer_follow_up_is_accepted():
    # Smoke run case 8 (CLM-2026-0111, hard fixture): get_claim, check_claim (clean) -> graph draft; then the
    # eval's reviewer replies and the model is called again. That call returned the 400 before the fix.
    cid = "CLM-2026-0111"
    script = iter([thought("get_claim", {"claim_id": cid}, 1), thought("check_claim", {"claim_id": cid}, 2),
                   AIMessage(content="Noted.", additional_kwargs={"reasoning_content": "..."})])
    async with mcp_tools(extra_env={**ENV, "REIMBURSE_BUDDY_DATA": str(HARD)}) as (_s, tools):
        g = build_graph(ThinkingProvider(messages=script), tools, checkpointer=InMemorySaver(), run_claim=cid)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}}
        await g.ainvoke({"messages": [("user", "Review claim CLM-2026-0111 and draft the decision note.")],
                         "declined": {}}, conf)
        st = await g.aget_state(conf)
        assert st.values["auto_drafts"][0]["args"] == {"claim_id": cid}
        await g.ainvoke({"messages": [("user", "Reviewer: I have no extra decision. Finish.")]}, conf)
        st = await g.aget_state(conf)
        assert st.values["messages"][-1].content == "Noted."


async def test_search_cap_two_per_gap_before_asking_then_one_more():
    cid = "CLM-2026-0203"  # one open gap (return boarding pass)
    env = {"REIMBURSE_BUDDY_DATA": str(HARD)}
    async with mcp_tools(extra_env={**ENV, **env}) as (_s, tools):
        chk = json.loads((await {t.name: t for t in tools}["check_claim"].ainvoke({"claim_id": cid}))[0]["text"])
    item, gap = next((i["item_id"], g) for i in chk["items"] for g in i.get("gaps", []))
    q = lambda i: call("search_reimburse_mailbox", {"employee_id": "EMP-005", "query": f"boarding pass try{i}"}, 10 + i)
    script = [call("check_claim", {"claim_id": cid}, 1), q(1), q(2), q(3),
              call("request_missing_evidence", {"claim_id": cid, "item_id": item, "gap_code": gap}, 4), q(4), q(5),
              AIMessage(content="final")]
    msgs = await _run(script, cid, env)
    searches = [m.content for m in msgs if m.type == "tool" and m.name == "search_reimburse_mailbox"]
    assert ["search limit" in x for x in searches] == [False, False, True, False, True]
