"""Recall@5 and MRR of the message each gap needs, on the large synthetic sets (Claude review 8, step 4c). $0, no LLM.

    uv run --extra embed python -m eval.retrieval_metrics            # writes eval/results/retrieval-<ts>.json

For each family the query is the one the no-LLM baseline sends (eval/baseline.py: item description, category
and gap words, date; the slang reply uses the post-ask query). Rankers:
  lexical   the server's own search_reimburse_mailbox (top 5) on clean_query(query)
  bm25      find_messages with RB_NO_EMBED (BM25 over the 36-probe pool)
  ranked    find_messages, BM25+embedding RRF over the 36-probe pool (the default)
  ranked_full  the same ranker over the whole mailbox: the ceiling if the pool were complete
Also: in_pool (did the 36 probes reach the message at all), pool coverage, possibly_incomplete, and for the
reschedule_earlier family the contradiction-scan similarity of the needed message vs the best distractor
(used to tune SIMILARITY_THRESHOLD on set A only).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from reimburse_agent import retrieval as R

from .baseline import CAT_WORDS, GAP_WORDS
from .fixture_hash import fixture_hash
from .hard_cases import store as _store, LARGE_CASES
from reimburse_buddy.data_store import DataStore

RESULTS = Path(__file__).parent / "results"
GAP = {"taxi_body": "receipt_missing", "slang_reply": "receipt_missing", "quoted": "receipt_missing",
       "reschedule_later": "late_checkout_unjustified", "reschedule_earlier": "late_checkout_unjustified"}


def rank_of(ids: list[str], needed: str) -> int | None:
    return ids.index(needed) + 1 if needed in ids else None


async def family_row(data: Path, fam: str, t: dict) -> dict:
    s = DataStore(data)
    if t["via_reply"]:
        s.inbox.append(next(r["reply"] for r in s.replies if r["claim_id"] == t["claim_id"]))
    claim = s.get_claim(t["claim_id"])
    it = next(i for i in claim["items"] if i["id"] == t["items"][0])
    gap = GAP[fam]
    words = " ".join(x for x in (it.get("description"), CAT_WORDS.get(it["category"], ""), GAP_WORDS.get(gap, ""), it.get("date")) if x)
    if t["via_reply"]:  # the baseline's post-ask query
        words = " ".join([t["claim_id"], it["id"], it.get("description", ""), CAT_WORDS.get(it["category"], ""), GAP_WORDS.get(gap, "")])
    emp = t["employee_id"]

    async def search(args):
        return s.search_reimburse_mailbox(emp, args.get("query", ""))

    lex = [m["id"] for m in s.search_reimburse_mailbox(emp, R.clean_query(words))]
    pool, incomplete = await R.message_pool(search, emp)
    mailbox = [m for m in s.inbox if m["owner_id"] == emp]
    big = 10_000
    ranked = [r["id"] for r in (await R.find_messages(search, emp, words, k=big))["results"]]

    async def full(args):  # every message as a server summary: the complete-pool ceiling
        return [{"id": m["id"], "date": m["date"], "from": m["from"], "subject": m["subject"], "snippet": m["body"][:160],
                 "attachments": [a.get("name", "") for a in m["attachments"]]} for m in mailbox]
    orig = R.message_pool
    R.message_pool = lambda _s, _e: _pool_all(full)
    try:
        ranked_full = [r["id"] for r in (await R.find_messages(search, emp, words, k=big))["results"]]
    finally:
        R.message_pool = orig
    row = {"family": fam, "claim": t["claim_id"], "needed": t["needed"], "query": words, "mailbox": len(mailbox),
           "pool": len(pool), "possibly_incomplete": incomplete, "in_pool": t["needed"] in {m["id"] for m in pool},
           "rank": {"lexical": rank_of(lex, t["needed"]), "ranked": rank_of(ranked, t["needed"]),
                    "ranked_full": rank_of(ranked_full, t["needed"])}}
    if fam == "reschedule_earlier":  # the graph's contradiction scan query: event title + date over the trip window
        ev = next(e for e in s.calendar if e["id"] == t["event"])
        q = f"{ev['title']} {ev['date']}"
        res = await R.find_messages(search, emp, q, claim["trip"]["start_date"], claim["trip"]["end_date"], k=big)
        sims = {r["id"]: r["similarity"] for r in res["results"]}
        row["scan"] = {"query": q, "needed_similarity": sims.get(t["needed"]),
                       "best_other_similarity": max([v for k, v in sims.items() if k != t["needed"] and v is not None], default=None),
                       "hits": R.contradiction_hits({"results": res["results"][:R.TOP_K]})}
    return row


async def _pool_all(full):
    return await full({}), False


def summarize(rows: list[dict], key: str) -> dict:
    ranks = [r["rank"][key] for r in rows]
    return {"recall@5": round(sum(1 for x in ranks if x and x <= 5) / len(ranks), 3),
            "mrr": round(sum(1 / x for x in ranks if x) / len(ranks), 3)}


async def run(no_embed: bool = False) -> dict:
    out = {}
    for name, cases in LARGE_CASES.items():
        data = cases[0].data
        truth = json.loads((data / "truth.json").read_text())
        rows = [await family_row(data, f, t) for f, t in truth.items()]
        out[name] = {"rows": rows, **{k: summarize(rows, k) for k in ("lexical", "ranked", "ranked_full")},
                     "ranker": R.embedder() and R.EMBED_MODEL or "bm25 only"}
    return out


def main():
    import argparse, os
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-embed", action="store_true")
    a = ap.parse_args()
    if a.no_embed:
        os.environ["RB_NO_EMBED"] = "1"
    res = asyncio.run(run())
    res["fixtures_sha256"] = fixture_hash()["sha256"]
    RESULTS.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = RESULTS / f"retrieval{'-bm25only' if a.no_embed else ''}-{ts}.json"
    p.write_text(json.dumps(res, indent=2, ensure_ascii=False))
    for name in LARGE_CASES:
        r = res[name]
        print(name, {k: r[k] for k in ("lexical", "ranked", "ranked_full")})
        for row in r["rows"]:
            print("  ", row["family"], row["rank"], "pool", row["pool"], "/", row["mailbox"], "in_pool", row["in_pool"],
                  "incomplete", row["possibly_incomplete"], row.get("scan", ""))
    print("saved", p)


if __name__ == "__main__":
    main()
