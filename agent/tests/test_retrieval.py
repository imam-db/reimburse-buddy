"""Step 5: wrapper matcher noise and find_messages (ranked search through the existing MCP search)."""

import json
from pathlib import Path

import pytest

from reimburse_agent import retrieval
from reimburse_agent.graph import attribute_query
from reimburse_agent.retrieval import bm25, clean_query, find_messages, message_pool, rrf

ROOT = Path(__file__).resolve().parents[2]
import sys  # noqa: E402
sys.path.insert(0, str(ROOT / "src"))
from reimburse_buddy.data_store import DataStore  # noqa: E402

FIXTURES = [ROOT / "data", ROOT / "agent/eval/fixtures/hard", ROOT / "agent/eval/fixtures/altered"]


def test_clean_query_compacts_ids_and_drops_times():
    assert clean_query("CLM-2026-0204 IT-5 taxi 15:00 2026-10-14") == "clm20260204 it5 taxi 20261014"
    assert clean_query("check-out at 9.30 wita") == "check-out at"


def test_attribute_query_uses_word_boundaries():
    check = {"items": [{"item_id": "IT-1", "category": "taxi"}, {"item_id": "IT-10", "category": "hotel"}]}
    gaps = ["IT-1:receipt_missing", "IT-10:hotel_nights_mismatch"]
    # 'passenger' is not 'pass', 'cabin' is not 'cab', 'IT-10' is not 'IT-1'
    assert attribute_query("cabin passenger IT-10", gaps, check) == "IT-10:hotel_nights_mismatch"
    assert attribute_query("taxi receipt IT-1", gaps, check) == "IT-1:receipt_missing"


@pytest.mark.parametrize("root", FIXTURES, ids=lambda p: p.name)
async def test_probe_sweep_recovers_every_message(root):
    s = DataStore(root)
    for emp in {m["owner_id"] for m in s.inbox}:
        async def search(a):
            return s.search_reimburse_mailbox(a["employee_id"], a["query"])
        rows, _inc = await message_pool(search, emp)
        got = {m["id"] for m in rows}
        assert got == {m["id"] for m in s.inbox if m["owner_id"] == emp}


def test_bm25_and_rrf():
    docs = ["taxi receipt airport", "hotel folio", "taxi"]
    sc = bm25("taxi receipt", docs)
    assert sc[0] > sc[2] > sc[1] == 0
    f = rrf([3, 2, 1], [2, 3, 1])
    assert f[0] == f[1] > f[2]


async def test_bm25_fallback_says_so(monkeypatch):
    monkeypatch.setattr(retrieval, "_EMBEDDER", None)
    monkeypatch.setattr(retrieval, "_EMBED_ERR", "offline")
    msgs = [{"id": "MSG-1", "date": "2026-10-01", "subject": "taxi receipt", "snippet": "", "attachments": []},
            {"id": "MSG-2", "date": "2026-11-01", "subject": "taxi", "snippet": "", "attachments": []}]

    async def search(a):
        return [m for m in msgs if a["query"] in json.dumps(m).lower()][:5]
    res = await find_messages(search, "E", "taxi receipt", date_to="2026-10-31")
    assert res["ranker"].startswith("bm25 only") and "offline" in res["ranker"]
    assert [r["id"] for r in res["results"]] == ["MSG-1"] and res["pool_size"] == 2
