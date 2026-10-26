"""Claude review 6 (fake models, no key, no network): budget exhaustion drafts from the engine, sub-runs get a
fixed small tool set, a fair-share budget and their own state slot (interrupt/resume, reviewer replies), and
the scorer only accepts writes with a matching approval."""

import copy
import uuid

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import reimburse_agent.batch as batch_mod
from eval.hard_cases import CASES
from eval.hard_score import score
from eval.run_hard_eval import run_one
from reimburse_agent.batch import SUBRUN_DROP, build_batch_graph, fair_share
from reimburse_agent.graph import BUDGET_STOP, build_graph, message_tokens
from reimburse_agent.mcp_client import mcp_tools

from test_graph_interrupt import Scripted, call
from test_review5 import ENV

BY = {c.id: c for c in CASES}
SUBS = ["CLM-2026-0101", "CLM-2026-0104", "CLM-2026-0110", "CLM-2026-0118"]


class Counting(Scripted):
    calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        type(self).calls += 1
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)


# --- (a) budget exhaustion -> engine draft, zero model tokens ------------------------------------------
async def test_budget_spent_before_any_check_drafts_from_a_no_evidence_check_without_the_model():
    cid = "CLM-2026-0110"
    Counting.calls = 0
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        g = build_graph(Counting(messages=iter([])), tools, checkpointer=InMemorySaver(), run_claim=cid, token_budget=0)
        v = await g.ainvoke({"messages": [("user", f"Review {cid}")], "declined": {}},
                            {"configurable": {"thread_id": str(uuid.uuid4())}})
    assert Counting.calls == 0 and v.get("tokens_used", 0) == 0
    ai = [m for m in v["messages"] if m.type == "ai"]
    assert ai[0].name == BUDGET_STOP
    assert ai[-1].content.startswith("needs human: model budget used; open gaps: ")
    d = v["auto_drafts"][-1]
    assert d["budget"] and d["check_id"] == "budget-no-check" and d["args"] == {"claim_id": cid}
    assert '"claim_id"' in d["content"] or cid in d["content"]  # draft_decision's own output


async def _batch(model, tools, answer=lambda req: True, **kw):
    g = build_batch_graph(model, tools, "2026-W43", checkpointer=InMemorySaver(), **kw)
    conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 120}
    seen = []

    async def turn(payload):
        while True:
            await g.ainvoke(payload, conf)
            st = await g.aget_state(conf)
            if not st.interrupts:
                return st
            req = st.interrupts[0].value
            seen.append(req)
            payload = Command(resume={"approved": answer(req), "tool_call_id": req["tool_call_id"], "reason": "no"})
    st = await turn({"messages": [("user", BY["3"].prompt)]})
    return g, conf, turn, st, seen


async def test_batch_with_no_budget_left_drafts_every_claim_from_the_engine_and_quotes_no_model_prose():
    Counting.calls = 0
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        _g, _c, _t, st, _seen = await _batch(Counting(messages=iter([])), tools, batch_token_budget=0)
    v = st.values
    assert Counting.calls == 0
    assert [s["claim_id"] for s in v["summaries"]] == SUBS
    assert all("stopped_with" not in s for s in v["summaries"])
    assert all(s["draft"] for s in v["summaries"])  # each claim got the engine's draft
    for cid in SUBS:
        sub = v["threads"][cid]["state"]
        assert sub["auto_drafts"][-1]["budget"]
    assert v["rp_unsupported"] == [] and "Warning: amounts" not in v["messages"][-1].content


def test_fair_share_splits_what_is_left_by_open_gaps_and_respects_the_cap():
    triage = '{"exceptions":[{"claim_id":"A","gaps":["x","y","z"]},{"claim_id":"B","gaps":[]}]}'
    assert fair_share(60000, ["A", "B"], triage, None) == 48000  # weights 4:1
    assert fair_share(60000, ["B", "A"], triage, None) == 12000
    assert fair_share(60000, ["A", "B"], triage, 35000) == 35000
    assert fair_share(10000, ["B"], triage, 35000) == 10000


def test_budget_counts_cache_miss_input_plus_output():
    m = AIMessage(content="x", response_metadata={"token_usage": {
        "prompt_tokens": 1000, "prompt_cache_hit_tokens": 800, "completion_tokens": 50}})
    assert message_tokens(m) == 250


# --- (b) fixed small sub-run tool set + token split ----------------------------------------------------
async def test_sub_runs_bind_a_fixed_tool_set_without_triage_policy_list_or_bulk_approval(monkeypatch):
    built = []
    real = batch_mod.build_graph

    def spy(model, tools, **kw):
        g = real(model, tools, **kw)
        built.append((kw.get("run_claim"), [t.name for t in tools], g.token_split))
        return g
    monkeypatch.setattr(batch_mod, "build_graph", spy)
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        await _batch(Scripted(messages=iter([AIMessage(content="ok")] * 8)), tools)
    assert [b[0] for b in built] == SUBS
    names = set(built[0][1])
    assert not names & SUBRUN_DROP and {"check_claim", "draft_decision", "search_reimburse_mailbox"} <= names
    assert all(set(b[1]) == names for b in built)  # static: identical for every claim
    split = built[0][2]
    assert set(split) == {"skill_md", "addendum", "tool_schemas", "tools", "served_text"}
    assert split["skill_md"] > 0 and split["addendum"] > 0 and split["tool_schemas"] > 0


# --- (a) interrupt in a middle claim, resume: no duplicated writes --------------------------------------
async def test_gated_interrupt_on_a_middle_claim_resumes_without_repeating_writes():
    """0101 sends a free receipt ask, then 0104 proposes a gated send_message (interrupt), and after resume
    0110 sends its own free ask. claim_run re-runs from the top on resume: nothing before sub.ainvoke may
    repeat, and 0101's finished sub-run must not run again."""
    sends = {"request_missing_evidence": 0, "send_message": 0}
    script = [
        call("check_claim", {"claim_id": SUBS[0]}, 1), AIMessage(content="0101 done."),
        call("check_claim", {"claim_id": SUBS[1]}, 2),
        call("send_message", {"to": "EMP-003", "subject": "Claim CLM-2026-0104", "body": "Please explain IT-6.",
                              "claim_id": SUBS[1]}, 3),
        AIMessage(content="0104 asked."),
        call("check_claim", {"claim_id": SUBS[2]}, 4),
        call("request_missing_evidence", {"claim_id": SUBS[2], "item_id": "IT-5", "gap_code": "receipt_missing"}, 5),
        AIMessage(content="0110 asked."),
        call("check_claim", {"claim_id": SUBS[3]}, 6), AIMessage(content="0118 done."),
    ]
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        def counted(t):
            if t.name not in sends:
                return t

            async def run(**kw):
                sends[t.name] += 1
                return await t.ainvoke(kw)
            return StructuredTool.from_function(coroutine=run, name=t.name, description=t.description,
                                                args_schema=t.args_schema)
        wrapped = [counted(t) for t in tools]
        _g, _c, _t, st, seen = await _batch(Scripted(messages=iter(script + [AIMessage(content="x")] * 6)), wrapped)
        by = {t.name: t for t in tools}
        out = await by["get_outbox"].ainvoke({})
    assert [r["tool"] for r in seen] == ["approve_cleared_batch", "send_message", "request_missing_evidence"]
    assert sends == {"request_missing_evidence": 1, "send_message": 1}
    w = [(x["tool"], x["args"].get("claim_id")) for x in st.values["writes"]]
    assert len(w) == len(set(w)) and ("send_message", SUBS[1]) in w and ("request_missing_evidence", SUBS[2]) in w
    assert [s["claim_id"] for s in st.values["summaries"]] == SUBS
    assert "0110" in str(out) and str(out).count("dokumen diperlukan") == 1


# --- (a) reviewer reply naming one claim goes to that claim's sub-run ----------------------------------
async def test_reviewer_reply_naming_a_claim_continues_that_sub_run():
    seen_prompts = []

    class Spy(Scripted):
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            seen_prompts.append([m.type for m in messages])
            if "what is still missing" in str(messages[-1].content):
                from langchain_core.outputs import ChatGeneration, ChatResult
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Noted for 0110."))])
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)

    script = [x for i, c in enumerate(SUBS) for x in (call("check_claim", {"claim_id": c}, 20 + i), AIMessage(content=f"{c}?"))]
    script += [AIMessage(content="ok")] * 6
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        g, conf, turn, st, _seen = await _batch(Spy(messages=iter(script)), tools)
        before = len(st.values["threads"]["CLM-2026-0110"]["state"]["messages"])
        st = await turn({"messages": [("user", "About CLM-2026-0110: what is still missing?")]})
    after = st.values["threads"]["CLM-2026-0110"]["state"]["messages"]
    assert len(after) > before and after[before].type == "human"
    assert seen_prompts[-1].count("tool") >= 1  # the model saw that claim's own earlier history
    assert "### CLM-2026-0110" in st.values["messages"][-1].content
    assert "Noted for 0110." in st.values["messages"][-1].content


# --- (d) independent scorer -----------------------------------------------------------------------------
async def test_scorer_flags_a_request_with_no_matching_approval():
    script = [call("check_claim", {"claim_id": SUBS[0]}, 70), AIMessage(content="0101 done.")]
    script += [x for i, c in enumerate(SUBS[1:]) for x in (call("check_claim", {"claim_id": c}, 80 + i),
                                                            AIMessage(content=f"{c} done."))]
    rec = await run_one("deepseek-flash", BY["3"], "", {"spent": 0.0, "limit": 1.0},
                        model=Scripted(messages=iter(script + [AIMessage(content="x")] * 8)))
    bad = copy.deepcopy(rec)
    bad["calls"].append({"id": "x1", "name": "request_missing_evidence", "args": {
        "claim_id": SUBS[0], "item_id": "IT-5", "gap_code": "receipt_missing"},
        "declined": False, "error": False, "turn": 9})
    m = score(BY["3"], bad, bad["final"], bad["tool_texts"],
              {"changed_status": bad["server_changed"], "outbox": 0})
    assert m["unauthorized_writes"] >= 1 and not m["safety"] and not m["pass"]
