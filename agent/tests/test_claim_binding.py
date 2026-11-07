"""A run investigates one claim: a write may only name that claim (graph.claim_binding).

A write staged for another claim is refused before the reviewer is asked and never reaches its executor;
the same write for the run's claim still stops at the approval gate as before.
"""

from langchain_core.messages import AIMessage

from reimburse_agent.mcp_client import mcp_tools

from test_approval_hardening import ENV, HERO, CASE6, counted, drive, WRITES


def _script(args):
    return [AIMessage(content="", tool_calls=[{"name": "update_claim_status", "args": args, "id": "w1"}]),
            AIMessage(content="done")]


async def test_write_for_another_claim_is_refused_before_the_reviewer_is_asked():
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        wrapped, calls = counted(tools)
        seen, _before, st = await drive(wrapped, _script({"claim_id": CASE6, "status": "on_hold"}),
                                        answer=lambda r: {"approved": True, "tool_call_id": r["tool_call_id"]},
                                        counts=calls)
    assert seen == []  # no interrupt: nothing to approve
    assert calls == {n: 0 for n in WRITES}
    assert not st.interrupts
    refusal = [m for m in st.values["messages"] if getattr(m, "tool_call_id", None) == "w1"]
    assert refusal and "not the claim this run investigates" in str(refusal[-1].content)


async def test_write_for_the_runs_claim_still_stops_at_the_gate():
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        wrapped, calls = counted(tools)
        seen, _before, _st = await drive(wrapped, _script({"claim_id": HERO, "status": "on_hold"}),
                                         answer=None, counts=calls)
    assert [r["tool"] for r in seen] == ["update_claim_status"]
    assert calls == {n: 0 for n in WRITES}


async def test_batch_approval_naming_another_claim_is_refused():
    script = [AIMessage(content="", tool_calls=[{"name": "approve_cleared_batch", "id": "w1", "args": {
        "batch_id": "2026-W43", "claim_ids": [HERO, CASE6], "reviewer_confirmed": True}}]),
        AIMessage(content="done")]
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        wrapped, calls = counted(tools)
        seen, _before, _st = await drive(wrapped, script, answer=None, counts=calls)
    assert seen == []
