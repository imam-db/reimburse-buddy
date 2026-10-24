"""Start the real stdio MCP server as a subprocess and talk to it with the MCP client."""

import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {"list_claims", "get_claim", "get_policy", "search_inbox", "get_calendar",
            "find_approval_memo", "check_claim", "draft_decision",
            "send_message", "request_approval", "update_claim_status", "get_outbox", "triage_claims"}


async def test_server_lists_tools_and_checks_claim():
    params = StdioServerParameters(command=sys.executable, args=["-m", "reimburse_buddy.server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert EXPECTED <= tools
            res = await session.call_tool("check_claim", {"claim_id": "CLM-2026-0101", "evidence_ids": ["MSG-5001"]})
            data = json.loads(res.content[0].text)
            assert data["decision"] == "approve"
            assert data["total_approvable"] == "6265000"
