"""No-LLM baseline (eval/baseline.py): $0, deterministic. It must pass the keyword-friendly cases and fail the
four cases built so that keyword search and "approve whatever the engine clears" go wrong."""
import pytest

from eval.baseline import BaselineModel
from eval.hard_cases import CASES
from eval.run_hard_eval import run_one
from reimburse_agent.graph import GAP_SEARCH_TOOL, SEARCH_TOOL, build_graph
from reimburse_agent.mcp_client import mcp_tools

from test_review5 import ENV

BY = {c.id: c for c in CASES}


async def _run(cid):
    return await run_one("baseline", BY[cid], "", {"spent": 0.0, "limit": 0.0}, model=BaselineModel())


@pytest.mark.parametrize("cid", ["1", "9"])
async def test_baseline_passes_keyword_friendly_cases(cid):
    rec = await _run(cid)
    assert rec["metrics"]["pass"], rec["metrics"]["why"]
    assert rec["usage"]["input"] == rec["usage"]["output"] == 0


@pytest.fixture
def bm25_only(monkeypatch):
    from reimburse_agent import retrieval
    monkeypatch.setattr(retrieval, "_EMBEDDER", None)
    monkeypatch.setattr(retrieval, "_EMBED_ERR", "disabled in test")


@pytest.mark.parametrize("cid", ["11", "12", "13", "14"])
async def test_lexical_baseline_fails_heuristic_hard_cases_but_stays_safe(cid, bm25_only):
    m = (await _run(cid))["metrics"]
    assert m["safety"] and not m["correct"] and not m["pass"]
    assert m["severity"] == ("false_approval" if cid == "14" else "incorrect")


@pytest.mark.parametrize("cid,passes", [("11", True), ("12", True), ("13", False), ("14", True)])
async def test_ranked_baseline_on_heuristic_hard_cases(cid, passes):
    """With find_messages (embedding model loaded) the baseline solves 11, 12, 14: their mailboxes hold 2-4
    messages, so the top 5 is the whole mailbox and the engine keeps what matches. 13 (11 messages, the
    receipt arrives as a reply found only by the lexical '<claim> <item>' search) still fails."""
    from reimburse_agent.retrieval import embedder
    if embedder() is None:
        pytest.skip("embedding model unavailable")
    m = (await _run(cid))["metrics"]
    assert m["safety"] and m["pass"] == passes, m["why"]


async def test_no_model_search_hides_search_tools_from_the_model():
    seen = {}

    class Spy(BaselineModel):
        def bind_tools(self, tools, **kw):
            seen["names"] = {getattr(t, "name", None) for t in tools}
            return super().bind_tools(tools, **kw)

    async with mcp_tools(extra_env=ENV) as (_s, tools):
        build_graph(Spy(), tools, model_search=False)
    assert SEARCH_TOOL not in seen["names"] and GAP_SEARCH_TOOL not in seen["names"]
    assert "check_claim" in seen["names"]
