"""Fake-script tests (no LLM, no key) for the graph's routing: clean check -> draft, mailbox search cache,
and exhausted gaps -> draft with the open items."""

import json
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


async def test_clean_check_goes_straight_to_draft():
    cid = "CLM-2026-0111"
    msgs = await _run([call("check_claim", {"claim_id": cid}, 1), AIMessage(content="final")], cid)
    assert tools_called(msgs) == ["check_claim", "draft_decision"]
    auto = [m for m in msgs if isinstance(m, AIMessage) and m.tool_calls and m.tool_calls[0]["id"].startswith("auto-draft")]
    assert len(auto) == 1 and msgs[-1].content == "final"  # the script had only two model turns


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
    msgs = await _run(script, cid, {"REIMBURSE_BUDDY_DATA": str(HARD)})
    assert tools_called(msgs)[-1] == "draft_decision"
    assert f"{item}:{gap}" in msgs[-2].content and "still open" in msgs[-2].content


def test_system_prompt_is_byte_identical_across_builds():
    a, b = load_system_prompt(), load_system_prompt()
    assert a == b and a.encode() == b.encode()
    assert "at most once per gap" not in a and "Never pick one receipt yourself" in a
