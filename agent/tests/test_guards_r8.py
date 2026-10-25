"""Claude review 8, step 2-3 (fake model, no key): one test per guard bypass.

- possibly_incomplete: a capped probe makes the contradiction scan unavailable -> review.
- 3a: an unrelated email ID passed alongside the calendar event does not switch the calendar-only guard off.
- 3b: the post-ask hold also runs when the model ends in plain text.
- 3c: the hold triggers on the fact (reply arrived, not considered), not on unused search budget.
- 3d: the mailbox snapshot is taken before the request, so messages already there are not "replies".
- 3e: hold once per mailbox version (tests/test_presearch_draft.py::test_held_once_per_mailbox_version_then_flagged).
"""

import json

from langchain_core.messages import AIMessage

from eval.hard_cases import CASES
from eval.run_hard_eval import run_one
from reimburse_agent import retrieval
from reimburse_agent.retrieval import message_pool

from test_graph_interrupt import Scripted
from test_review7 import C9, CID as CID9, RUN2

C14 = next(c for c in CASES if c.id == "14")
CID = C14.claim_id


def _msgs(turns, tag):
    return [AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"{tag}{t}c{i}"} for i, (n, a) in enumerate(c)])
            for t, c in enumerate(turns)]


async def _run(case, turns, tag, tail="Done."):
    return await run_one("deepseek-flash", case, "", {"spent": 0.0, "limit": 1.0},
                         model=Scripted(messages=iter(_msgs(turns, tag) + [AIMessage(content=tail)] * 3)))


async def test_message_pool_flags_a_capped_probe():
    msgs = [{"id": f"MSG-{i}"} for i in range(5)]

    async def capped(_a):
        return msgs
    rows, inc = await message_pool(capped, "E")
    assert len(rows) == 5 and inc

    async def small(_a):
        return msgs[:4]
    assert (await message_pool(small, "E"))[1] is False


async def test_incomplete_pool_sends_calendar_only_item_to_review(monkeypatch):
    real = retrieval.message_pool

    async def incomplete(search, emp):
        rows, _ = await real(search, emp)
        return rows, True
    monkeypatch.setattr(retrieval, "message_pool", incomplete)
    monkeypatch.setattr(retrieval, "contradiction_hits", lambda res, threshold=0: [])  # nothing flagged by content
    import reimburse_agent.graph as g
    monkeypatch.setattr(g, "contradiction_hits", lambda res, threshold=0: [])
    rec = await _run(C14, [[("get_claim", {"claim_id": CID})], [("check_claim", {"claim_id": CID, "evidence_ids": ["EVT-7341"]})],
                           [("check_claim", {"claim_id": CID, "evidence_ids": ["EVT-7341", "MSG-9341"]})]], "i")
    assert rec["reviewer_turns"] == 1
    assert any("unavailable" in t and "possibly incomplete" in t for t in rec.get("graph_texts", [])), rec.get("graph_texts")


async def test_unrelated_email_alongside_does_not_bypass_calendar_guard():
    # MSG-9311 (same employee, another claim's reschedule) passed with the event: still calendar-only for IT-4
    turns = [[("get_claim", {"claim_id": CID})],
             [("check_claim", {"claim_id": CID, "evidence_ids": ["EVT-7341", "MSG-9311"]})],
             [("draft_decision", {"claim_id": CID, "evidence_ids": ["EVT-7341", "MSG-9311"]})]]
    rec = await _run(C14, turns, "u")
    stops = [t for t in rec["tool_texts"] if '"needs_review"' in t] + [
        t for t in rec.get("graph_texts", []) if t.startswith("Not drafted")]
    assert stops and "MSG-9341" in stops[0] and "IT-4" in stops[0], (stops, rec.get("graph_texts"))
    assert rec["metrics"]["false_approvals"] == []


async def test_model_draft_with_unrelated_email_is_held():
    """Model drafts straight away (no check first that could route to the graph's own scan)."""
    turns = [[("get_claim", {"claim_id": CID})],
             [("check_claim", {"claim_id": CID})],
             [("draft_decision", {"claim_id": CID, "evidence_ids": ["EVT-7341", "MSG-9311"]})]]
    rec = await _run(C14, turns, "m")
    held = [t for t in rec["tool_texts"] if '"needs_review"' in t]
    assert held and "MSG-9341" in held[0] and "IT-4" in held[0]
    assert rec["metrics"]["false_approvals"] == []


async def test_plain_text_ending_after_ask_reports_the_unchecked_reply():
    rec = await _run(C9, RUN2[:4] + [RUN2[4][:2]], "t", tail="I have asked the employee; waiting.")
    texts = rec.get("graph_texts", [])
    assert any("Not final" in t and "MSG-9401" in t for t in texts), texts


async def test_hold_is_on_the_fact_not_the_budget_and_snapshot_precedes_the_request():
    # post-ask budget used by the model's own (useless) searches: still held, and only true replies are named
    turns = RUN2[:4] + [RUN2[4][:2],
                        [("search_for_gap", {"item_id": "IT-5", "gap_code": "receipt_missing", "query": "zzz a"}),
                         ("search_for_gap", {"item_id": "IT-8", "gap_code": "receipt_missing", "query": "zzz b"})],
                        [("draft_decision", {"claim_id": CID9, "evidence_ids": ["MSG-5003"]})]]
    rec = await _run(C9, turns, "f")
    held = json.loads(next(t for t in rec["tool_texts"] if "new_evidence_found" in t))
    assert set(held["new_evidence_ids"]) == {"MSG-9401", "MSG-9402"}
