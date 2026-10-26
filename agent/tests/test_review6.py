"""Review 6 (fake models, no key, no network): free evidence requests inside batch sub-runs are scored as
free, sub-runs get triage + policy handed in and served, and each sub-run has a model token budget."""

import copy
import uuid

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

from eval.hard_cases import CASES
from eval.hard_score import score
from eval.run_hard_eval import run_one
from reimburse_agent.batch import build_batch_graph, policy_summary
from reimburse_agent.graph import BUDGET_STOP, build_graph
from reimburse_agent.mcp_client import mcp_tools

from test_graph_interrupt import Scripted, call
from test_review5 import ENV, drive

BY = {c.id: c for c in CASES}
SUBS = ["CLM-2026-0101", "CLM-2026-0104", "CLM-2026-0110", "CLM-2026-0118"]


def _case3_script(extra_0110=()):
    s = []
    for i, cid in enumerate(SUBS):
        s.append(call("check_claim", {"claim_id": cid}, 10 * i))
        if cid == "CLM-2026-0110":
            s.extend(extra_0110)
        s.append(AIMessage(content=f"{cid} needs you."))
    return s


FREE_0110 = call("request_missing_evidence", {"claim_id": "CLM-2026-0110", "item_id": "IT-5",
                                              "gap_code": "receipt_missing"}, 99)


async def _case3_rec():
    return await run_one("deepseek-flash", BY["3"], "", {"spent": 0.0, "limit": 1.0},
                         model=Scripted(messages=iter(_case3_script([FREE_0110]))))


def _rescore(rec):
    srv = {"changed_status": rec["server_changed"], "outbox": 0}
    return score(BY["3"], rec, rec["final"], rec["tool_texts"], srv)


# --- 1. every evidence request is gated, including a batch sub-run's ---------------------------------------
async def test_evidence_request_in_a_batch_sub_run_is_gated_and_authorized():
    rec = await _case3_rec()
    ev = [c for c in rec["calls"] if c["name"] == "request_missing_evidence"]
    gate = [g for g in rec["gated"] if g["tool"] == "request_missing_evidence"]
    assert ev and gate and gate[0]["approved"]
    assert gate[0]["args"]["claim_id"] == "CLM-2026-0110" and not ev[0].get("declined")
    assert rec["metrics"]["unauthorized_writes"] == 0 and rec["metrics"]["safety"]


async def test_scorer_flags_a_request_without_an_approval():
    rec = await _case3_rec()
    bad = copy.deepcopy(rec)
    bad["calls"].append({"id": "x1", "name": "request_missing_evidence", "args": {
        "claim_id": "CLM-2026-0104", "item_id": "IT-6", "gap_code": "receipt_missing"},
        "declined": False, "error": False, "turn": 9})
    assert _rescore(bad)["unauthorized_writes"] == 1
    good = copy.deepcopy(bad)
    good["gated"].append({"tool": "request_missing_evidence", "args": good["calls"][-1]["args"],
                          "tool_call_id": "x1", "approved": True})
    assert _rescore(good)["unauthorized_writes"] == 0


# --- 2. slimmer sub-runs -------------------------------------------------------------------------------
async def test_sub_runs_get_triage_and_policy_and_repeat_calls_are_served_not_fetched():
    hits = {"triage_claims": 0, "get_policy": 0}
    prompts = []

    class Spy(Scripted):
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            prompts.append(next(str(m.content) for m in messages if m.type == "human"))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)

    script = [call("triage_claims", {"batch": "2026-W43"}, 1), call("get_policy", {}, 2), *_case3_script()]
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        def counted(t):
            if t.name not in hits:
                return t

            async def run(**kw):
                hits[t.name] += 1
                return await t.ainvoke(kw)
            return StructuredTool.from_function(coroutine=run, name=t.name, description=t.description,
                                                args_schema=t.args_schema)
        wrapped = [counted(t) for t in tools]
        g = build_batch_graph(Spy(messages=iter(script)), wrapped, "2026-W43", checkpointer=InMemorySaver())
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 80}
        from langgraph.types import Command
        payload = {"messages": [("user", BY["3"].prompt)]}
        while True:
            await g.ainvoke(payload, conf)
            st = await g.aget_state(conf)
            if not st.interrupts:
                break
            payload = Command(resume={"approved": True, "tool_call_id": st.interrupts[0].value["tool_call_id"]})
    assert hits == {"triage_claims": 1, "get_policy": 1}  # the parent's one fetch each; the sub-run's were served
    first = prompts[0]
    assert "Triage row for CLM-2026-0101" in first and '"hotel_nightly_cap"' in first and "rules" not in first
    assert len(st.values["summaries"]) == 4
    tools_seen = [m for m in st.values["messages"]]
    assert tools_seen  # parent history holds only the request and the code-built final text
    assert all(not getattr(m, "tool_calls", None) for m in st.values["messages"])  # no fake tool-call messages


def test_policy_summary_keeps_numbers_drops_prose():
    s = policy_summary('{"currency":"IDR","hotel_nightly_cap":{"1":"1200000"},"rules":["long text"],"company":"X"}')
    assert s == '{"currency":"IDR","hotel_nightly_cap":{"1":"1200000"}}'


# --- per-run token budget ------------------------------------------------------------------------------
async def test_token_budget_stops_with_plain_text_and_the_runner_does_not_count_it():
    cid = "CLM-2026-0110"
    loop = [call("check_claim", {"claim_id": cid}, i) for i in range(20)]
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        g = build_graph(Scripted(messages=iter(loop)), tools, checkpointer=InMemorySaver(), run_claim=cid,
                        token_budget=3000)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 80}
        v = await g.ainvoke({"messages": [("user", f"Review {cid}")], "declined": {}}, conf)
    ai = [m for m in v["messages"] if m.type == "ai"]
    # review 6 (Claude): the graph's stop text is followed by an engine draft, not left undrafted
    assert ai[-2].name == BUDGET_STOP and not ai[-2].tool_calls and "model budget" in ai[-2].content
    assert ai[-1].content.startswith("needs human: model budget used; open gaps: IT-1:boarding_pass_missing")
    assert v["auto_drafts"][-1]["budget"] and len(ai) < 20 and v["tokens_used"] >= 3000


async def test_batch_budget_still_finishes_with_every_claim_summarised():
    loop = [call("check_claim", {"claim_id": c}, 100 + i) for c in SUBS for i in range(6)]
    v, _seen = await drive(Scripted(messages=iter(loop + [AIMessage(content="done")] * 40)), None,
                           [BY["3"].prompt], batch="2026-W43")
    assert [s["claim_id"] for s in v["summaries"]] == SUBS
    assert "### CLM-2026-0118" in v["messages"][-1].content
