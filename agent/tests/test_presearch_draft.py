"""Post-ask hold (fake model, no key): the model drafts after asking the employee. The graph compares the
mailbox with its snapshot from just before the request; a reply the latest check_claim did not consider
holds the draft (once per mailbox version)."""

from langchain_core.messages import AIMessage

from eval.run_hard_eval import run_one

from test_graph_interrupt import Scripted
from test_review7 import C9, CID, RUN2


def _msgs(turns, tag):
    return [AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"{tag}{t}c{i}"} for i, (n, a) in enumerate(c)])
            for t, c in enumerate(turns)]


async def test_model_draft_is_held_when_the_reply_is_already_in_the_mailbox():
    turns = RUN2[:4] + [RUN2[4][:2], [("draft_decision", {"claim_id": CID, "evidence_ids": ["MSG-5003"]})],
                        [("check_claim", {"claim_id": CID, "evidence_ids": ["MSG-5003", "MSG-9401", "MSG-9402"]})]]
    rec = await run_one("deepseek-flash", C9, "", {"spent": 0.0, "limit": 1.0},
                        model=Scripted(messages=iter(_msgs(turns, "p") + [AIMessage(content="Done.")] * 3)))
    held = next(c for c in rec["calls"] if c["name"] == "draft_decision" and not c.get("auto"))
    held_text = next(t for t in rec["tool_texts"] if "new_evidence_found" in t)
    assert "re-check before drafting" in held_text and "MSG-9401" in held_text and "MSG-9402" in held_text
    assert held["error"]  # not executed
    m = rec["metrics"]
    assert m["pass"], m["why"]
    # no graph-made tool-call message: the only search_for_gap calls are the model's four
    assert sum(c["name"] == "search_for_gap" for c in rec["calls"]) == 4


async def test_held_once_per_mailbox_version_then_flagged():
    """The model searched but never re-checked with the reply: the first draft is held (a fact: a reply
    arrived after the ask and was not considered), a repeat draft on the same mailbox runs, flagged."""
    turns = RUN2[:4] + [RUN2[4][:2],
                        [("search_for_gap", {"item_id": "IT-5", "gap_code": "receipt_missing", "query": "zzz one"}),
                         ("search_for_gap", {"item_id": "IT-8", "gap_code": "receipt_missing", "query": "zzz two"})],
                        [("draft_decision", {"claim_id": CID, "evidence_ids": ["MSG-5003"]})],
                        [("draft_decision", {"claim_id": CID, "evidence_ids": ["MSG-5003"]})]]
    rec = await run_one("deepseek-flash", C9, "", {"spent": 0.0, "limit": 1.0},
                        model=Scripted(messages=iter(_msgs(turns, "q") + [AIMessage(content="Done.")] * 3)))
    d = [c for c in rec["calls"] if c["name"] == "draft_decision" and not c.get("auto")]
    assert len(d) == 2 and d[0]["error"] and not d[1]["error"]
    assert sum("new_evidence_found" in t for t in rec["tool_texts"]) == 1
    assert any("new evidence found but not used" in t for t in rec["tool_texts"])


async def test_draft_runs_when_the_reply_was_considered():
    turns = RUN2[:4] + [RUN2[4][:2],
                        [("check_claim", {"claim_id": CID, "evidence_ids": ["MSG-5003", "MSG-9401", "MSG-9402"]})],
                        [("draft_decision", {"claim_id": CID, "evidence_ids": ["MSG-5003", "MSG-9401", "MSG-9402"]})]]
    rec = await run_one("deepseek-flash", C9, "", {"spent": 0.0, "limit": 1.0},
                        model=Scripted(messages=iter(_msgs(turns, "r") + [AIMessage(content="Done.")] * 3)))
    assert not any("new_evidence_found" in t for t in rec["tool_texts"])
    assert rec["metrics"]["pass"], rec["metrics"]["why"]
