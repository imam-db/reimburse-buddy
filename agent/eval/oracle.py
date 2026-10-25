"""Oracle trajectories for the heuristic-hard cases 11-14: hand-written ideal tool sequences, run through the
real graph, server, budget and scorer with a scripted (fake) model. $0, no key.

    python -m eval.oracle            # each oracle passes? + which plausible queries find the needed message

An oracle passing means the case is solvable by SOME agent under the current tools and the 2+1 search budget.
The query probe shows how narrow that is: each case lists the words a reviewer could plausibly try first
(from the claim's own fields, in English and Indonesian) and whether lexical search puts the needed message
in its top 5. The oracle query itself is chosen with hindsight; it is marked as such."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from .hard_cases import CASES

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from reimburse_buddy.data_store import DataStore  # noqa: E402
from reimburse_agent import retrieval  # noqa: E402
from reimburse_agent.retrieval import clean_query, embedder, find_messages  # noqa: E402


class _Scripted(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


def _sg(item, gap, query):
    return ("search_for_gap", {"item_id": item, "gap_code": gap, "query": query})


LC = "late_checkout_unjustified"
RM = "receipt_missing"
# case -> (turns, needed message, the hindsight query the oracle uses, plausible first queries)
ORACLES = {
    "11": ([[("get_claim", {"claim_id": "CLM-2026-0301"})],
            [("check_claim", {"claim_id": "CLM-2026-0301"})],
            [("get_calendar", {"employee_id": "EMP-003", "date_from": "2026-10-15", "date_to": "2026-10-16"}),
             _sg("IT-4", LC, "jadwal meeting klien")],
            [("check_claim", {"claim_id": "CLM-2026-0301", "evidence_ids": ["EVT-7301", "MSG-9311"]})]],
           "MSG-9311", "jadwal meeting klien",
           ["late checkout meeting moved", "meeting rescheduled", "meeting diundur", "late checkout hotel Surabaya",
            "jadwal meeting", "meeting PT Arunika Niaga"]),
    "12": ([[("get_claim", {"claim_id": "CLM-2026-0302"})],
            [("check_claim", {"claim_id": "CLM-2026-0302"})],
            [_sg("IT-3", RM, "bukti bayar taksi bandara")],
            [("check_claim", {"claim_id": "CLM-2026-0302", "evidence_ids": ["MSG-9321"]})]],
           "MSG-9321", "bukti bayar taksi bandara",
           ["taxi receipt", "taxi airport hotel receipt", "struk taksi", "kuitansi taksi bandara", "bukti bayar taksi",
            "Taxi airport to hotel taxi taksi receipt struk kuitansi 2026-10-13"]),
    "13": ([[("get_claim", {"claim_id": "CLM-2026-0303"})],
            [("check_claim", {"claim_id": "CLM-2026-0303"})],
            [_sg("IT-3", RM, "taxi hotel airport receipt struk")],
            [("request_missing_evidence", {"claim_id": "CLM-2026-0303", "item_id": "IT-3", "gap_code": RM})],
            [_sg("IT-3", RM, "bon")],
            [("check_claim", {"claim_id": "CLM-2026-0303", "evidence_ids": ["MSG-9331"]})]],
           "MSG-9331", "bon",
           ["CLM-2026-0303 IT-3", "taxi receipt", "struk taksi", "receipt", "nota taksi", "bon taksi"]),
    "14": ([[("get_claim", {"claim_id": "CLM-2026-0304"})],
            [("check_claim", {"claim_id": "CLM-2026-0304"})],
            [("get_calendar", {"employee_id": "EMP-003", "date_from": "2026-10-08", "date_to": "2026-10-09"}),
             _sg("IT-4", LC, "Partner review PT Bahari")],
            [("check_claim", {"claim_id": "CLM-2026-0304", "evidence_ids": ["EVT-7341", "MSG-9341"]})],
            "IT-4's late checkout: the calendar says the review ran 13:00-14:30, but MSG-9341 says it was moved "
            "to 08:00-10:00. Which decision for IT-4?",
            [("check_claim", {"claim_id": "CLM-2026-0304", "evidence_ids": ["EVT-7341", "MSG-9341"]})]],
           "MSG-9341", "Partner review PT Bahari",
           ["late checkout meeting moved", "meeting rescheduled", "late checkout hotel Makassar", "meeting dimajukan",
            "Partner review with PT Bahari Sejahtera", "jadwal review"]),
}


def script(turns) -> list[AIMessage]:
    out = []
    for t, calls in enumerate(turns):
        if isinstance(calls, str):
            out.append(AIMessage(content=calls))
        else:
            out.append(AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"o{t}c{i}"}
                                                         for i, (n, a) in enumerate(calls)]))
    return out + [AIMessage(content="Done.")] * 3


def budget_ok(turns) -> bool:
    """Every gap's searches stay within 2 before the ask and 1 after (the graph enforces it too)."""
    per: dict = {}
    for calls in turns:
        if isinstance(calls, str):
            continue
        for n, a in calls:
            k = (a.get("item_id"), a.get("gap_code"))
            if n == "request_missing_evidence":
                per.setdefault(k, [0, 0, False])[2] = True
            elif n == "search_for_gap":
                row = per.setdefault(k, [0, 0, False])
                row[1 if row[2] else 0] += 1
    return all(b <= 2 and a <= 1 for b, a, _ in per.values())


def _store(case):
    s = DataStore(case.data)
    for r in s.replies:  # replies already delivered: the probe asks "would the search find it"
        if r.get("claim_id") == case.claim_id:
            s.inbox.append(dict(r["reply"]))
    return s, s.get_claim(case.claim_id)["employee_id"]


def probe(case, queries, needed, search=None) -> dict[str, bool]:
    """query -> needed message in lexical search's top 5 (the server's own matcher on the wrapper's cleaned
    query, fixture data). search: optional (store, employee_id, query) -> list of IDs."""
    s, emp = _store(case)
    search = search or (lambda st, e, q: [h["id"] for h in st.search_reimburse_mailbox(e, clean_query(q))])
    return {q: needed in search(s, emp, q) for q in queries}


async def probe_find(case, queries, needed) -> dict[str, bool]:
    """query -> rank of the needed message in find_messages' full ranking (no date window) / mailbox size."""
    s, emp = _store(case)

    async def raw(a):
        return s.search_reimburse_mailbox(a["employee_id"], a["query"])
    out = {}
    for q in queries:
        res = await find_messages(raw, emp, q, k=99)
        ids = [r["id"] for r in res["results"]]
        out[q] = f"rank {ids.index(needed) + 1}/{res['in_window']}" if needed in ids else f"miss/{res['in_window']}"
    return out


async def run_oracles(cases=("11", "12", "13", "14")) -> dict:
    from .run_hard_eval import run_one
    out = {}
    for cid in cases:
        turns, needed, oq, plausible = ORACLES[cid]
        case = next(c for c in CASES if c.id == cid)
        rec = await run_one("oracle", case, "", {"spent": 0.0, "limit": 0.0}, model=_Scripted(messages=iter(script(turns))))
        m = rec["metrics"]
        out[cid] = {"pass": m["pass"], "why": m["why"], "severity": m["severity"], "budget_ok": budget_ok(turns),
                    "searches": sum(c["name"] == "search_for_gap" for c in rec["calls"]),
                    "oracle_query (hindsight)": oq, "plausible_queries_hit": probe(case, plausible, needed),
                    "find_messages_rank (top 5 = hit)": await probe_find(case, plausible, needed),
                    "find_messages_ranker": "bm25+embedding rrf" if embedder() else f"bm25 only ({retrieval._EMBED_ERR})",
                    "error": rec["error"]}
    return out


if __name__ == "__main__":
    from .fixture_hash import fixture_hash
    from .run_eval import RESULTS
    tag = sys.argv[1] if len(sys.argv) > 1 else "lexical"
    res = {"fixtures_sha256": fixture_hash()["sha256"], "oracles": asyncio.run(run_oracles())}
    (RESULTS / f"oracle-{tag}.json").write_text(json.dumps(res, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(res, indent=2, ensure_ascii=False))
