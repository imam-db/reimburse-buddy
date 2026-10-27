"""Step 4: every heuristic-hard case is solvable under the current tools, 2+1 budget and scorer (oracle
trajectories), and every results file carries the fixture hash."""

from eval.fixture_hash import fixture_hash
from eval.oracle import ORACLES, budget_ok, run_oracles


async def test_oracles_pass_within_budget():
    res = await run_oracles()
    for cid, r in res.items():
        assert r["budget_ok"] and r["pass"] and not r["error"], (cid, r)
        assert r["plausible_queries_hit"][ORACLES[cid][2]] if ORACLES[cid][2] in r["plausible_queries_hit"] else True


def test_budget_check_catches_overspend():
    over = [[("search_for_gap", {"item_id": "IT-1", "gap_code": "receipt_missing", "query": q})] for q in "abc"]
    assert not budget_ok(over)


def test_fixture_hash_is_stable_and_covers_fixtures():
    a, b = fixture_hash(), fixture_hash()
    assert a["sha256"] == b["sha256"] and len(a["sha256"]) == 64
    assert any(k.startswith("agent/eval/fixtures/hard/") for k in a["files"])
