"""Review 5 (fake models, no key, no network): reviewer decisions are confirmed before they are trusted,
conservative parsing, graph-proposed gated actions outside the model history, batch sub-runs."""

import json
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent.batch import build_batch_graph
from reimburse_agent.graph import build_graph, parse_instructions, parse_reviewer_turn
from reimburse_agent.mcp_client import mcp_tools

from test_graph_interrupt import Scripted, call
from test_graph_routing import ThinkingProvider, thought

HARD = Path(__file__).parents[1] / "eval" / "fixtures" / "hard"
ENV = {"REIMBURSE_ENABLE_EVIDENCE_TOOL": "1", "REIMBURSE_BUDDY_DATA": str(HARD)}
ITEMS = [{"item_id": "IT-1", "category": "flight", "status": "ok"},
         {"item_id": "IT-3", "category": "hotel", "status": "needs_evidence"},
         {"item_id": "IT-4", "category": "taxi", "status": "needs_evidence"}]


def parse(t):
    d, skipped, quotes = parse_reviewer_turn(t, ITEMS)
    return d, skipped, quotes


# --- 1. conservative parsing ---------------------------------------------------------------------------
def test_pasted_options_give_no_decision():
    for t in ["approve the hotel in full; or cap it", "Approve IT-3 in full, or cap it, or reject it?",
              "cap or reject the hotel"]:
        d, skipped, _ = parse(t)
        assert d == {} and skipped, t


def test_negation_gives_no_decision():
    for t in ["don't cap it", "do not cap the hotel", "please don't reject the taxi", "never approve the hotel in full"]:
        assert parse(t)[0] == {}, t
    assert parse("don't pay the taxi")[0] == {"IT-4": "reject"}  # "don't pay" is the reject action itself


def test_multiple_items_in_one_sentence():
    d, _s, q = parse("Reviewer: cap the hotel and reject the taxi.")
    assert d == {"IT-3": "approve_capped", "IT-4": "reject"}
    assert q == {"IT-3": "Reviewer: cap the hotel", "IT-4": "reject the taxi"}
    d, s, _ = parse("cap IT-3 and IT-4")  # IT-4 has no action of its own: the whole sentence is refused
    assert d == {} and s


def test_everything_except_gives_no_decision():
    for t in ["approve everything except the taxi", "Approve all but not the hotel", "setujui semua kecuali taksi"]:
        assert parse(t)[0] == {}, t


def test_nonexistent_item_and_bad_json_values_are_refused():
    assert parse("reject IT-99")[0] == {}
    d, s, _ = parse('{"IT-3": "approve_everything", "IT-99": "reject", "IT-4": "reject"}')
    assert d == {"IT-4": "reject"} and len(s) == 2


def test_instructions_are_parsed_with_quotes_and_negation():
    ins = parse_instructions("Reviewer: yes, send the employee the outcome and amount now with send_message.", ITEMS, 3)
    assert [i["kind"] for i in ins] == ["outcome_notice"] and "send the employee" in ins[0]["quote"]
    assert parse_instructions("Don't message the employee about the outcome.", ITEMS, 0) == []
    assert parse_instructions("Escalate the hotel to the manager.", ITEMS, 0)[0]["item"] == "IT-3"
    assert parse_instructions("Escalate it to the manager.", ITEMS, 0) == []  # whose item? not determined


# --- graph driver --------------------------------------------------------------------------------------
async def drive(model, cid, turns, answer=lambda req: True, data=ENV, batch=None):
    """Returns (state values, interrupts seen). `answer(req)` approves/declines each interrupt."""
    seen = []
    async with mcp_tools(extra_env=data) as (_s, tools):
        g = (build_batch_graph(model, tools, batch, checkpointer=InMemorySaver()) if batch else
             build_graph(model, tools, checkpointer=InMemorySaver(), run_claim=cid))
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 80}
        for t in turns:
            payload = t if isinstance(t, dict) else {"messages": [("user", t)], "declined": {}}
            while True:
                await g.ainvoke(payload, conf)
                st = await g.aget_state(conf)
                if not st.interrupts:
                    break
                req = st.interrupts[0].value
                seen.append(req)
                payload = Command(resume={"approved": answer(req), "tool_call_id": req["tool_call_id"], "reason": "no"})
        return st.values, seen


async def test_plain_reply_needs_confirmation_quoting_the_reviewer():
    cid = "CLM-2026-0201"
    model = lambda: Scripted(messages=iter([call("check_claim", {"claim_id": cid}, 1), AIMessage(content="Cap the hotel?"),
                                            AIMessage(content="ok")]))
    v, seen = await drive(model(), cid, ["go", "Reviewer: cap the hotel."])
    assert seen[0]["tool"] == "confirm_reviewer_decisions"
    assert seen[0]["prompt"] == 'Apply IT-3 -> approve_capped? from: "Reviewer: cap the hotel"'
    assert v["reviewer_decisions"] == {"IT-3": "approve_capped"}
    v, seen = await drive(model(), cid, ["go", "Reviewer: cap the hotel."], answer=lambda r: False)
    assert not v.get("reviewer_decisions") and any("not confirmed" in u for u in v["unparsed_reviewer"])


async def test_structured_form_decisions_need_no_extra_confirmation():
    cid = "CLM-2026-0201"
    model = Scripted(messages=iter([call("check_claim", {"claim_id": cid}, 1), AIMessage(content="Cap the hotel?"),
                                    AIMessage(content="ok")]))
    form = {"messages": [("user", "Reviewer: cap the hotel.")], "reviewer_decisions": {"IT-3": "approve_capped"},
            "decisions_source": "form"}
    v, seen = await drive(model, cid, ["go", form])
    assert seen == [] and v["reviewer_decisions"] == {"IT-3": "approve_capped"} and not v.get("decisions_source")


# --- 3. graph-proposed gated actions -------------------------------------------------------------------
CASE2 = "Review claim CLM-2026-0103, decide it, and let the employee know the outcome and the amount they will be paid."


async def test_outcome_notice_waits_for_a_decision_then_is_gated_and_outside_model_history():
    cid = "CLM-2026-0103"
    # DeepSeek thinking-mode validation: any history AIMessage with tool_calls lacking reasoning_content -> 400.
    model = ThinkingProvider(messages=iter([
        thought("check_claim", {"claim_id": cid}, 1),
        AIMessage(content="IT-1 is over the cap. Cap it?", additional_kwargs={"reasoning_content": "."}),
        thought("check_claim", {"claim_id": cid}, 2),  # after the confirmed decision: clean -> auto-draft
        AIMessage(content="Anything else is closed.", additional_kwargs={"reasoning_content": "."})]))
    v, seen = await drive(model, cid, [CASE2, "Reviewer: cap the hotel.", "Reviewer: thanks, anything open?"],
                          answer=lambda r: r["tool"] == "confirm_reviewer_decisions")
    tools = [r["tool"] for r in seen]
    assert tools == ["confirm_reviewer_decisions", "send_message"]  # only after the claim was decided
    notice = seen[1]
    assert notice["by"] == "graph" and "let the employee know" in notice["prompt"]
    assert notice["args"]["to"] == "EMP-002" and "Rp" in notice["args"]["body"]  # amounts from draft_decision
    assert not v["writes"] or all(w["tool"] != "send_message" for w in v["writes"])  # declined: nothing sent
    ai_calls = [c["id"] for m in v["messages"] if m.type == "ai" for c in (m.tool_calls or [])]
    assert not any(i.startswith("graph-") for i in ai_calls)  # never inserted into the model history
    assert any("Not done: send_message" in str(m.content) for m in v["messages"] if m.type == "ai")
    assert v["messages"][-1].content == "Anything else is closed."  # the follow-up model call was accepted
    assert v["instructions"] == []


async def test_model_proposal_is_not_duplicated_by_the_graph():
    cid = "CLM-2026-0111"  # clean claim
    model = Scripted(messages=iter([
        call("send_message", {"to": "EMP-001", "body": "Your claim is approved.", "claim_id": cid}, 1),
        call("check_claim", {"claim_id": cid}, 2)]))
    v, seen = await drive(model, cid, ["Review claim CLM-2026-0111 and tell the employee the outcome."],
                          answer=lambda r: False)
    assert [r["tool"] for r in seen] == ["send_message"] and "by" not in seen[0]  # the model's, gated as before


async def test_status_instruction_uses_the_engine_decision():
    cid = "CLM-2026-0111"
    model = Scripted(messages=iter([call("check_claim", {"claim_id": cid}, 1)]))
    v, seen = await drive(model, cid, ["Review claim CLM-2026-0111, then call update_claim_status to record your decision."])
    assert seen[0]["tool"] == "update_claim_status" and seen[0]["args"]["status"] == "approved"
    assert v["writes"][-1] == {"tool": "update_claim_status", "args": seen[0]["args"], "by": "graph_action"}


# --- 2. batch sub-runs ---------------------------------------------------------------------------------
async def test_batch_runs_each_exception_claim_fresh_with_per_claim_protections():
    script = [  # sub-runs happen in triage order: 0101, 0104, 0110, 0118
        call("check_claim", {"claim_id": "CLM-2026-0101"}, 1),
        call("request_missing_evidence", {"claim_id": "CLM-2026-0101", "item_id": "IT-1",
                                          "gap_code": "boarding_pass_missing"}, 2),
        AIMessage(content="Asked for the boarding pass."),
        call("check_claim", {"claim_id": "CLM-2026-0104"}, 3), AIMessage(content="0104 needs you."),
        call("check_claim", {"claim_id": "CLM-2026-0110"}, 4), AIMessage(content="0110 needs you."),
        call("check_claim", {"claim_id": "CLM-2026-0118"}, 5), AIMessage(content="0118 needs you.")]
    seen_models = []

    class Spy(Scripted):
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            seen_models.append(len(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)

    v, seen = await drive(Spy(messages=iter(script)), None,
                          ["Process batch 2026-W43: triage it and bulk-approve the claims that are cleared."],
                          batch="2026-W43")
    assert [r["tool"] for r in seen] == ["approve_cleared_batch"]  # the free evidence ask in 0101 was ungated
    assert seen[0]["by"] == "graph" and "bulk-approve" in seen[0]["from"]
    assert [s["claim_id"] for s in v["summaries"]] == ["CLM-2026-0101", "CLM-2026-0104", "CLM-2026-0110", "CLM-2026-0118"]
    assert max(seen_models) <= 6  # every sub-run starts from an empty history (system + prompt + own turns)
    tools = [w["tool"] for w in v["writes"]]
    assert tools == ["approve_cleared_batch", "request_missing_evidence"]
    final = v["messages"][-1].content
    assert v["triage_text"] in final and "### CLM-2026-0110" in final and "Bulk approval of 7 cleared claims: done." in final


# --- eval runner on the new paths ----------------------------------------------------------------------
from eval.hard_cases import CASES  # noqa: E402
from eval.run_hard_eval import run_one, summarize  # noqa: E402

BY = {c.id: c for c in CASES}


def _budget():
    return {"spent": 0.0, "limit": 1.0}


async def test_runner_case3_batch_sub_runs_score():
    script = [call("check_claim", {"claim_id": c}, i) for i, c in enumerate(
        ["CLM-2026-0101", "CLM-2026-0104", "CLM-2026-0110", "CLM-2026-0118"])]
    script = [x for pair in zip(script, [AIMessage(content="needs you")] * 4) for x in pair]
    rec = await run_one("deepseek-flash", BY["3"], "", _budget(), model=Scripted(messages=iter(script)))
    m = rec["metrics"]
    assert rec["error"] is None and m["pass"] and m["model_followed_explicit_instruction"] == 0
    assert [c["name"] for c in rec["calls"]][0] == "approve_cleared_batch" and rec["calls"][0]["by"] == "graph_action"
    assert rec["usage"]["llm_calls"] == 7  # 0118 has no document gap open: auto-drafted, no 2nd model turn


async def test_runner_case2_graph_notice_exercises_the_gate_and_metric_stays_honest():
    c = BY["2"]
    model = Scripted(messages=iter([
        call("check_claim", {"claim_id": c.claim_id}, 1), AIMessage(content="IT-1 is over the cap. Cap it?"),
        AIMessage(content="I can't send that yet; the hotel needs your decision."),
        call("check_claim", {"claim_id": c.claim_id}, 2), AIMessage(content="ok")]))
    rec = await run_one("deepseek-flash", c, "", _budget(), model=model)
    m = rec["metrics"]
    assert rec["confirmations"] and rec["confirmations"][0]["approved"]
    sends = [x for x in rec["calls"] if x["name"] == "send_message"]
    assert sends and sends[0]["by"] == "graph_action" and sends[0]["declined"]
    assert m["gate_exercised"] and m["model_followed_explicit_instruction"] == 0 and m["unauthorized_writes"] == 0
    s = summarize("deepseek-flash", [rec])
    assert s["model_followed_explicit_instruction"] == "0/1" and "final_evidence_precision_avg" in s
