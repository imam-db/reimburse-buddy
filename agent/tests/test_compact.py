"""No LLM: compact mode keeps every ID, gap and money figure while shrinking outputs."""

import json
import re

from reimburse_agent.compact import compact, tool_text
from reimburse_agent.mcp_client import mcp_tools

NUM = re.compile(r"\d+")


async def test_compact_keeps_facts_and_shrinks():
    calls = [("check_claim", {"claim_id": "CLM-2026-0110"}), ("triage_claims", {"batch": "2026-W43"}),
             ("get_claim", {"claim_id": "CLM-2026-0110"}),
             ("search_reimburse_mailbox", {"employee_id": "EMP-001", "query": "boarding pass"})]
    async with mcp_tools() as (_s, tools):
        by = {t.name: t for t in tools}
        raw_total = small_total = 0
        for name, args in calls:
            res = await by[name].ainvoke(args)
            raw = json.dumps(res, default=str, ensure_ascii=False)
            small = compact(tool_text(res))
            json.loads(small)
            raw_total, small_total = raw_total + len(raw), small_total + len(small)
            for tok in ("CLM-2026-0110", "hotel_over_cap", "4840000", "MSG-5001"):
                if tok in tool_text(res):
                    assert tok in small, (name, tok)
        assert small_total < 0.7 * raw_total


def test_compact_rules():
    data = {"a": None, "b": [], "findings": [{"rule": "r", "message": "m", "key": "k", "params": {"x": 1},
                                               "resolved_by": None, "gap": "g"}], "snippet": "x" * 400}
    out = json.loads(compact(json.dumps(data)))
    assert out["findings"] == [{"rule": "r", "message": "m", "gap": "g"}]
    assert "a" not in out and "b" not in out and len(out["snippet"]) == 283
    assert compact("plain note") == "plain note"
