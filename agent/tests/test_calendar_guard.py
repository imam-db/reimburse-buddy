"""Step 3 (fake model, no key): a late checkout that check_claim passes on a calendar event alone needs one
mailbox search attributed to that gap before the graph (or the model) drafts. Case 14: the search built from
the event title finds the email that moved the meeting earlier, so nothing is approved without the reviewer."""

from langchain_core.messages import AIMessage

from eval.hard_cases import CASES
from eval.run_hard_eval import run_one
from reimburse_agent.graph import calendar_only

from test_graph_interrupt import Scripted

C14 = next(c for c in CASES if c.id == "14")
CID = C14.claim_id


def _msgs(turns, tag):
    return [AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"{tag}{t}c{i}"} for i, (n, a) in enumerate(c)])
            for t, c in enumerate(turns)]


def test_calendar_only_detection():
    row = {"item_id": "IT-4", "evidence_used": ["EVT-1"], "findings": [{"rule": "late_checkout", "status": "ok"}]}
    assert calendar_only({"items": [row]}) == {"IT-4:late_checkout_unjustified": ["EVT-1"]}
    assert calendar_only({"items": [{**row, "evidence_used": ["EVT-1", "MSG-2"]}]}) == {}
    assert calendar_only({"items": [row]}, {"IT-4": "reject"}) == {}


async def test_auto_draft_path_asks_the_reviewer_instead_of_approving():
    turns = [[("get_claim", {"claim_id": CID})],
             [("check_claim", {"claim_id": CID, "evidence_ids": ["EVT-7341"]})],
             # after the reviewer's "reject the late checkout": re-check with what the graph reported
             [("check_claim", {"claim_id": CID, "evidence_ids": ["EVT-7341", "MSG-9341"]})]]
    rec = await run_one("deepseek-flash", C14, "", {"spent": 0.0, "limit": 1.0},
                        model=Scripted(messages=iter(_msgs(turns, "g") + [AIMessage(content="Done.")] * 3)))
    m = rec["metrics"]
    assert rec["reviewer_turns"] == 1  # the graph stopped and asked instead of drafting an approval
    assert m["false_approvals"] == [] and m["pass"], m["why"]


async def test_model_draft_is_held_on_a_calendar_only_justification():
    turns = [[("get_claim", {"claim_id": CID})],
             [("check_claim", {"claim_id": CID})],
             [("draft_decision", {"claim_id": CID, "evidence_ids": ["EVT-7341"]})]]
    rec = await run_one("deepseek-flash", C14, "", {"spent": 0.0, "limit": 1.0},
                        model=Scripted(messages=iter(_msgs(turns, "h") + [AIMessage(content="Done.")] * 3)))
    held = [t for t in rec["tool_texts"] if "calendar-only justification" in t]
    assert held and "MSG-9341" in held[0]
    assert rec["metrics"]["false_approvals"] == []
