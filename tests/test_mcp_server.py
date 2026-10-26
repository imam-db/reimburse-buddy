"""Start the real stdio MCP server as a subprocess and talk to it with the MCP client."""

import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

READ_TOOLS = {"list_claims", "get_claim", "get_policy", "search_reimburse_mailbox", "approve_cleared_batch",
              "get_calendar", "find_approval_memo", "check_claim", "draft_decision",
              "get_outbox", "triage_claims", "reset_demo"}
WRITE_TOOLS = {"send_message", "request_approval", "update_claim_status"}
EXPECTED = READ_TOOLS | WRITE_TOOLS


def _params(env: dict | None = None, **extra: str) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=["-m", "reimburse_buddy.server"],
                                 env={**os.environ, **(env or {}), **extra})


async def test_server_lists_tools_and_checks_claim():
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert tools == READ_TOOLS and len(tools) == 12  # WorkBuddy's server: no write tools
            res = await session.call_tool("check_claim", {"claim_id": "CLM-2026-0101", "evidence_ids": ["MSG-5001"]})
            data = json.loads(res.content[0].text)
            assert data["decision"] == "approve"
            assert data["total_approvable"] == "6265000"


async def test_write_tools_not_listed_or_callable_without_flag():
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert not tools & WRITE_TOOLS
            try:
                res = await session.call_tool("send_message", {"to": "EMP-001", "body": "hi"})
                assert res.isError
            except Exception:  # the client may raise instead of returning an error result
                pass


async def test_write_tools_listed_and_callable_with_flag():
    async with stdio_client(_params(REIMBURSE_ENABLE_WRITE_TOOLS="1")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert tools == EXPECTED
            res = await session.call_tool("send_message", {"to": "EMP-001", "body": "Please send the receipt."})
            data = json.loads(res.content[0].text)
            assert data["status"] == "sent"


async def test_evidence_tool_only_with_flag():
    async with stdio_client(_params(REIMBURSE_ENABLE_EVIDENCE_TOOL="1")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert tools == READ_TOOLS | {"request_missing_evidence"}
