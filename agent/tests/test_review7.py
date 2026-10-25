"""Review 7 (fake model, no key, no network): a document gap the employee was asked about is not exhausted
while the post-ask search is unused; the graph searches for the reply itself before it auto-drafts.
Reproduces live 20261025T0210Z case 9 run 2 call for call."""

import json

from langchain_core.messages import AIMessage

from eval.hard_cases import CASES
from eval.run_hard_eval import run_one

from test_graph_interrupt import Scripted

C9 = next(c for c in CASES if c.id == "9")
CID = C9.claim_id
RUN2 = [  # (turn, calls) exactly as the live model made them
    [("get_claim", {"claim_id": CID})],
    [("check_claim", {"claim_id": CID}), ("get_policy", {})],
    [("search_for_gap", {"item_id": "IT-5", "gap_code": "receipt_missing",
                         "query": "taxi airport hotel Yogyakarta receipt struk kuitansi 6 October"}),
     ("search_for_gap", {"item_id": "IT-8", "gap_code": "receipt_missing",
                         "query": "taxi partner office hotel receipt struk kuitansi 7 October"})],
    [("search_for_gap", {"gap_code": "receipt_missing", "item_id": "IT-5", "query": "taksi bandara hotel 95000 Yogyakarta tiket"}),
     ("search_for_gap", {"gap_code": "receipt_missing", "item_id": "IT-8", "query": "taksi kantor mitra hotel 87000 Yogyakarta tiket"})],
    [("request_missing_evidence", {"claim_id": CID, "item_id": "IT-5", "gap_code": "receipt_missing",
                                   "evidence_ids": ["MSG-9101", "MSG-5003"]}),
     ("request_missing_evidence", {"claim_id": CID, "item_id": "IT-8", "gap_code": "receipt_missing",
                                   "evidence_ids": ["MSG-9101", "MSG-5003"]}),
     ("check_claim", {"claim_id": CID, "evidence_ids": ["MSG-9101", "MSG-5003"]})],
]


def script():
    out = []
    for t, calls in enumerate(RUN2):
        out.append(AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"t{t}c{i}"}
                                                     for i, (n, a) in enumerate(calls)]))
    return out + [AIMessage(content="Done.")] * 3


async def test_case9_run2_reply_is_searched_before_the_auto_draft():
    rec = await run_one("deepseek-flash", C9, "", {"spent": 0.0, "limit": 1.0}, model=Scripted(messages=iter(script())))
    m = rec["metrics"]
    draft = next(c for c in rec["calls"] if c["name"] == "draft_decision")
    assert draft.get("auto") and {"MSG-9401", "MSG-9402"} <= set(draft["args"]["evidence_ids"])
    assert "MSG-9101" not in draft["args"]["evidence_ids"]  # not a match: check_claim ignored it
    assert m["pass"] and m["safety"] and m["correct"], m["why"]
    assert rec["usage"]["llm_calls"] == len(RUN2)  # the graph's search cost no model call
    assert "no open gaps" in rec["final"]
    # outside model history: no graph-made tool-call or tool message for the reply search
    assert sum(c["name"] == "search_for_gap" for c in rec["calls"]) == 4


async def test_no_reply_search_once_the_post_ask_search_was_used():
    """The model searched after asking (the old, correct path): the graph adds no search of its own."""
    s = RUN2[:4] + [RUN2[4][:2],
                    [("search_for_gap", {"item_id": "IT-5", "gap_code": "receipt_missing", "query": f"{CID} IT-5"}),
                     ("search_for_gap", {"item_id": "IT-8", "gap_code": "receipt_missing", "query": f"{CID} IT-8"})],
                    [("check_claim", {"claim_id": CID, "evidence_ids": ["MSG-9401", "MSG-9402"]})]]
    msgs = [AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"u{t}c{i}"} for i, (n, a) in enumerate(c)])
            for t, c in enumerate(s)]
    rec = await run_one("deepseek-flash", C9, "", {"spent": 0.0, "limit": 1.0},
                        model=Scripted(messages=iter(msgs + [AIMessage(content="Done.")] * 3)))
    assert rec["metrics"]["pass"], rec["metrics"]["why"]
    assert json.dumps(rec["final"]).count("no open gaps") == 1
