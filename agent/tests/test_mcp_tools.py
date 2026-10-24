"""No LLM: load the real tools over MCP stdio and call them directly."""

import json

from reimburse_agent.mcp_client import SERVER_ENTRYPOINT, mcp_tools

EXPECTED = {"list_claims", "get_claim", "get_policy", "search_reimburse_mailbox", "get_calendar",
            "find_approval_memo", "check_claim", "triage_claims", "approve_cleared_batch",
            "draft_decision", "send_message", "request_approval", "update_claim_status",
            "get_outbox", "reset_demo"}


def _json(res):
    if isinstance(res, tuple):  # (content, artifact) in some adapter versions
        res = res[0]
    if isinstance(res, list):
        res = res[0]["text"] if isinstance(res[0], dict) else res[0]
    return json.loads(res) if isinstance(res, str) else res


async def test_entrypoint_and_hero_claim_without_evidence():
    assert SERVER_ENTRYPOINT == "reimburse-buddy-mcp"
    async with mcp_tools() as (_s, tools):
        by = {t.name: t for t in tools}
        assert EXPECTED <= set(by)
        data = _json(await by["check_claim"].ainvoke({"claim_id": "CLM-2026-0110"}))
        assert data["total_approvable"] == "4840000"  # Rp4.840.000 before any evidence
        assert data["decision"] == "needs_info"
