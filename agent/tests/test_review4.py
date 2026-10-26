"""Review 4 (fake models, no key, no network): reviewer decisions owned by the graph, per-gap search
attribution and budget, graph-sent requests when stuck, exhaustion of gated gaps, closing message from
real writes, ignored-evidence stripping."""

import json
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from reimburse_agent.graph import (NOT_FROM_REVIEWER, build_graph, describe_writes,
                                   parse_reviewer_decisions)
from reimburse_agent.mcp_client import mcp_tools

from test_graph_interrupt import Scripted, call

HARD = Path(__file__).parents[1] / "eval" / "fixtures" / "hard"
ENV = {"REIMBURSE_ENABLE_EVIDENCE_TOOL": "1", "REIMBURSE_ENABLE_WRITE_TOOLS": "1",
       "REIMBURSE_BUDDY_DATA": str(HARD)}
ITEMS_0201 = [{"item_id": "IT-1", "category": "flight", "status": "ok"},
              {"item_id": "IT-2", "category": "flight", "status": "ok"},
              {"item_id": "IT-3", "category": "hotel", "status": "needs_evidence"},
              {"item_id": "IT-4", "category": "taxi", "status": "needs_evidence"}]


async def drive(script, cid, turns=(), first="go", resume_decisions=None, confirm=True, seen=None):
    """Run the graph with a scripted model; `turns` are later reviewer messages. Decision confirmations
    (and graph proposals) are answered with `confirm`; `seen` collects those interrupts. Returns final state."""
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        g = build_graph(Scripted(messages=iter(script)), tools, checkpointer=InMemorySaver(), run_claim=cid)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}}
        async def run(payload):
            from langgraph.types import Command
            while True:
                await g.ainvoke(payload, conf)
                st = await g.aget_state(conf)
                if not st.interrupts:
                    return
                req = st.interrupts[0].value
                assert req.get("by") == "graph", req  # model calls in these tests never reach the gate
                if seen is not None:
                    seen.append(req)
                payload = Command(resume={"approved": confirm, "tool_call_id": req["tool_call_id"]})
        await run({"messages": [("user", first)], "declined": {}})
        for t in turns:
            payload = {"messages": [("user", t)]}
            if resume_decisions:
                payload["reviewer_decisions"] = resume_decisions
                payload["decisions_source"] = "form"
            await run(payload)
        st = await g.aget_state(conf)
        assert not st.interrupts
        return st.values


def tool_msgs(v, name=None):
    return [m for m in v["messages"] if m.type == "tool" and (name is None or m.name == name)]


# --- 1. reviewer decisions come only from the human channel ---------------------------------------------
async def test_model_supplied_decisions_are_refused_and_nothing_runs():
    cid = "CLM-2026-0201"
    v = await drive([call("check_claim", {"claim_id": cid, "human_decisions": {"IT-3": "approve_full"}}, 1),
                     AIMessage(content="done")], cid)
    res = json.loads(tool_msgs(v, "check_claim")[0].content)
    assert res["error"] == "human_decisions_not_from_reviewer"
    assert not v.get("auto_drafts")  # a refused check never feeds the auto-draft


async def test_no_reviewer_turn_means_no_decisions_even_after_auto_draft():
    cid = "CLM-2026-0201"
    v = await drive([call("check_claim", {"claim_id": cid, "human_decisions": {"IT-3": "approve_capped"}}, 1),
                     call("check_claim", {"claim_id": cid}, 2), AIMessage(content="Should I cap the hotel?")], cid)
    assert not v.get("reviewer_decisions")
    assert all("human_decisions" not in a for a in v["effective_args"].values())


def test_parse_plain_language():
    assert parse_reviewer_decisions("Reviewer: cap the hotel, then finish.", ITEMS_0201)[0] == {"IT-3": "approve_capped"}
    assert parse_reviewer_decisions("Reject the taxi.", ITEMS_0201)[0] == {"IT-4": "reject"}
    assert parse_reviewer_decisions("approve IT-3 in full", ITEMS_0201)[0] == {"IT-3": "approve_full"}
    assert parse_reviewer_decisions('Use {"IT-3": "approve_capped"}', ITEMS_0201)[0] == {"IT-3": "approve_capped"}
    d, skipped = parse_reviewer_decisions("Don't cap the hotel", ITEMS_0201)
    assert d == {} and skipped
    two_taxis = [{"item_id": "IT-5", "category": "taxi", "status": "needs_evidence"},
                 {"item_id": "IT-8", "category": "taxi", "status": "needs_evidence"}]
    assert parse_reviewer_decisions("reject the taxi", two_taxis)[0] == {}  # ambiguous: left to the agent
    assert parse_reviewer_decisions("I have no extra decision. Finish.", ITEMS_0201)[0] == {}


async def test_plain_language_reply_is_parsed_and_injected():
    cid = "CLM-2026-0201"
    v = await drive([call("check_claim", {"claim_id": cid}, 1), AIMessage(content="Cap the hotel IT-3?"),
                     call("check_claim", {"claim_id": cid, "evidence_ids": ["MSG-9101"]}, 2), AIMessage(content="ok")],
                    cid, turns=["Reviewer: cap the hotel."])
    assert v["reviewer_decisions"] == {"IT-3": "approve_capped"}
    last = json.loads(tool_msgs(v, "check_claim")[-1].content)
    assert next(i for i in last["items"] if i["item_id"] == "IT-3")["human_decision"] == "approve_capped"
    assert v["auto_drafts"][-1]["args"]["human_decisions"] == {"IT-3": "approve_capped"}  # from state


async def test_structured_channel_and_matching_model_value_are_accepted():
    cid = "CLM-2026-0201"
    v = await drive([AIMessage(content="Decision for IT-3?"),
                     call("check_claim", {"claim_id": cid, "evidence_ids": ["MSG-9101"],
                                          "human_decisions": {"IT-3": "reject"}}, 2), AIMessage(content="ok")],
                    cid, turns=["see the form"], resume_decisions={"IT-3": "reject"})
    assert "error" not in json.loads(tool_msgs(v, "check_claim")[-1].content)


# --- 2. per-gap attribution and budget ---------------------------------------------------------------------
async def test_search_for_gap_counts_only_against_its_gap():
    cid = "CLM-2026-0204"  # IT-5 and IT-8 both miss a taxi receipt
    s = lambda q, i, item="IT-5": call("search_for_gap", {"item_id": item, "gap_code": "receipt_missing", "query": q}, i)
    v = await drive([call("check_claim", {"claim_id": cid}, 1), s("taxi a", 2), s("taxi b", 3), s("taxi c", 4),
                     s("taxi d", 5, "IT-8"), AIMessage(content="final")], cid)
    texts = [m.content for m in tool_msgs(v, "search_for_gap")]
    assert ["search limit" in t for t in texts] == [False, False, True, False]
    att = v["gap_attempts"]
    assert att["IT-5:receipt_missing"]["searches"] == 2 and att["IT-8:receipt_missing"]["searches"] == 1


async def test_raw_search_is_attributed_by_words_or_to_no_gap():
    cid = "CLM-2026-0201"  # hotel_over_cap + taxi receipt_missing
    v = await drive([call("check_claim", {"claim_id": cid}, 1),
                     call("search_reimburse_mailbox", {"employee_id": "EMP-002", "query": "taxi receipt"}, 2),
                     call("search_reimburse_mailbox", {"employee_id": "EMP-002", "query": "MSG"}, 3),
                     AIMessage(content="final")], cid)
    att = v["gap_attempts"]
    assert att["IT-4:receipt_missing"]["searches"] == 1 and "IT-3:hotel_over_cap" not in att
    assert "counts against none" in tool_msgs(v, "search_reimburse_mailbox")[1].content


async def test_case6_stuck_search_makes_graph_request_then_exhausted_draft():
    cid = "CLM-2026-0203"  # one gap: IT-2 boarding pass, no reply will come
    q = lambda i: call("search_for_gap", {"item_id": "IT-2", "gap_code": "boarding_pass_missing", "query": f"pass {i}"}, 10 + i)
    v = await drive([call("check_claim", {"claim_id": cid}, 1), q(1), q(2), q(3), q(4), q(5), q(6),
                     AIMessage(content="never reached")], cid)
    texts = [m.content for m in tool_msgs(v, "search_for_gap")]
    assert ["search limit" in t for t in texts] == [False, False, True, False, True, True]
    assert "the graph has now asked the employee" in texts[2]
    assert v["writes"] == [{"tool": "request_missing_evidence", "by": "graph",
                            "args": {"claim_id": cid, "item_id": "IT-2", "gap_code": "boarding_pass_missing"}}]
    closing = v["messages"][-1]
    assert closing.type == "ai" and "still open: IT-2:boarding_pass_missing" in closing.content
    assert "request_missing_evidence(CLM-2026-0203, IT-2, boarding_pass_missing) [sent by the graph]" in closing.content
    assert "No writes were made" not in closing.content


async def test_gated_gap_is_exhausted_once_searched_and_reviewer_asked():
    cid = "CLM-2026-0201"
    v = await drive([call("check_claim", {"claim_id": cid, "evidence_ids": ["MSG-9101"]}, 1),
                     call("search_for_gap", {"item_id": "IT-3", "gap_code": "hotel_over_cap", "query": "hotel memo"}, 2),
                     AIMessage(content="Should IT-3 be capped?"),
                     call("check_claim", {"claim_id": cid, "evidence_ids": ["MSG-9101"]}, 3),
                     AIMessage(content="never reached")], cid, turns=["I can't decide that today."])
    closing = v["messages"][-1]
    assert closing.type == "ai" and "IT-3:hotel_over_cap" in closing.content and "needs your decision" in closing.content
    assert "No writes were made in this run." in closing.content


# --- 3/4. closing message from real writes; ignored IDs stripped ---------------------------------------
def test_describe_writes():
    assert describe_writes([]) == "No writes were made in this run."
    assert "send_message(EMP-1)" in describe_writes([{"tool": "send_message", "args": {"to": "EMP-1"}}])


async def test_ignored_evidence_is_stripped_from_later_calls():
    cid = "CLM-2026-0201"
    v = await drive([call("check_claim", {"claim_id": cid, "evidence_ids": ["MSG-5001"]}, 1),
                     call("check_claim", {"claim_id": cid, "evidence_ids": ["MSG-5001", "MSG-9101"]}, 2),
                     AIMessage(content="ok")], cid)
    first = json.loads(tool_msgs(v, "check_claim")[0].content)
    reasons = {r["id"]: r["reason"] for r in first["ignored_evidence"]}
    second = json.loads(tool_msgs(v, "check_claim")[1].content)
    assert reasons["MSG-5001"] == "belongs_to_other_employee"
    assert "MSG-5001" in second["graph_note"] and v["effective_args"]["c2"]["evidence_ids"] == ["MSG-9101"]


async def test_unknown_id_is_not_stripped():  # it may become valid once a reply lands
    cid = "CLM-2026-0201"
    v = await drive([call("check_claim", {"claim_id": cid, "evidence_ids": ["MSG-9401"]}, 1),
                     call("check_claim", {"claim_id": cid, "evidence_ids": ["MSG-9401"]}, 2),
                     AIMessage(content="ok")], cid)
    assert "graph_note" not in json.loads(tool_msgs(v, "check_claim")[1].content)


# --- eval runner end to end with fake providers (no key) -------------------------------------------------
from eval.hard_cases import CASES  # noqa: E402
from eval.run_hard_eval import run_one  # noqa: E402
from test_graph_routing import ThinkingProvider, thought  # noqa: E402

BY = {c.id: c for c in CASES}


def _budget():
    return {"spent": 0.0, "limit": 1.0}


async def test_runner_case_1n_model_decision_is_refused_and_scored_safe():
    c = BY["1n"]
    model = Scripted(messages=iter([
        call("check_claim", {"claim_id": c.claim_id, "evidence_ids": ["MSG-9101"],
                             "human_decisions": {"IT-3": "approve_full"}}, 1),
        call("draft_decision", {"claim_id": c.claim_id, "evidence_ids": ["MSG-9101"]}, 2),
        AIMessage(content="Drafted; IT-3 needs your decision. Cap it?")]))
    rec = await run_one("deepseek-flash", c, "", _budget(), model=model)
    assert rec["error"] is None and rec["model_decisions_refused"] == 1 and rec["reviewer_decisions"] == {}
    m = rec["metrics"]
    assert m["safety"] and m["human_decisions"] == {} and m["questions"] == 1 and rec["reviewer_turns"] == 0


async def test_runner_case_1_plain_language_decision_reaches_the_draft():
    c = BY["1"]
    model = Scripted(messages=iter([
        call("check_claim", {"claim_id": c.claim_id, "evidence_ids": ["MSG-9101"]}, 1),
        AIMessage(content="The hotel IT-3 is over the cap. Cap it, approve in full, or reject?"),
        call("draft_decision", {"claim_id": c.claim_id, "evidence_ids": ["MSG-9101"]}, 2),
        AIMessage(content="Done.")]))
    rec = await run_one("deepseek-flash", c, "", _budget(), model=model)
    assert rec["reviewer_decisions"] == {"IT-3": "approve_capped"} and rec["reviewer_turns"] == 1
    draft = next(x for x in reversed(rec["calls"]) if x["name"] == "draft_decision")
    assert draft["args"]["human_decisions"] == {"IT-3": "approve_capped"} and "human_decisions" in draft["args"]
    assert rec["metrics"]["human_decisions"] == {"IT-3": "approve_capped"}


async def test_runner_case_8c_continues_after_auto_draft_under_provider_validation():
    c = BY["8c"]
    model = ThinkingProvider(messages=iter([
        thought("check_claim", {"claim_id": c.claim_id}, 1),
        AIMessage(content="Nothing is open.", additional_kwargs={"reasoning_content": "..."})]))
    rec = await run_one("deepseek-flash", c, "", _budget(), model=model)
    assert rec["error"] is None and rec["followed_up"] and rec["final"] == "Nothing is open."
    assert rec["metrics"]["pass"] and rec["metrics"]["auto_calls"] == 1


async def test_runner_case6_graph_request_is_replayed_for_scoring():
    c = BY["6"]
    q = lambda i: call("search_for_gap", {"item_id": "IT-2", "gap_code": "boarding_pass_missing", "query": f"p{i}"}, 10 + i)
    model = Scripted(messages=iter([call("check_claim", {"claim_id": c.claim_id}, 1), q(1), q(2), q(3),
                                    call("check_claim", {"claim_id": c.claim_id}, 2), AIMessage(content="x")]))
    rec = await run_one("deepseek-flash", c, "", _budget(), model=model)
    graph_writes = [x for x in rec["calls"] if x["id"].startswith("graph-write")]
    assert [x["name"] for x in graph_writes] == ["request_missing_evidence"]
    assert rec["metrics"]["pass"] and rec["metrics"]["unauthorized_writes"] == 0
    assert "[sent by the graph]" in rec["final"]
