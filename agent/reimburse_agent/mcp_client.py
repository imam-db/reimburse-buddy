"""Start the existing reimburse-buddy MCP server over stdio, exactly as WorkBuddy does.

Command: uv --directory <repo root> run --frozen --no-dev reimburse-buddy-mcp
(`reimburse-buddy-mcp` is the [project.scripts] entry point in the root pyproject.toml.)
`--no-dev` keeps the server environment to its runtime dependency (`mcp`) only.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_NAME = "reimburse-buddy"
SERVER_ENTRYPOINT = "reimburse-buddy-mcp"


def server_connection(repo_root: Path = REPO_ROOT, extra_env: dict | None = None) -> dict:
    """extra_env: e.g. {"REIMBURSE_BUDDY_DATA": <eval fixture dir>}; the default data is untouched."""
    return {
        "transport": "stdio",
        "command": "uv",
        "args": ["--directory", str(repo_root), "run", "--frozen", "--no-dev", SERVER_ENTRYPOINT],
        # Pass PATH etc. through, but no model keys: the server does not need them.
        "env": {k: v for k, v in os.environ.items() if not k.startswith("REIMBURSE_AGENT_")
                and not k.endswith("_API_KEY") and k != "VIRTUAL_ENV"}
        | {"REIMBURSE_ENABLE_EVIDENCE_TOOL": "1"}  # version B only; WorkBuddy never sets it
        | (extra_env or {}),
    }


@asynccontextmanager
async def mcp_tools(repo_root: Path = REPO_ROOT, extra_env: dict | None = None):
    """Yield (session, tools). One session = one fresh server process = clean demo state."""
    client = MultiServerMCPClient({SERVER_NAME: server_connection(repo_root, extra_env)})
    async with client.session(SERVER_NAME) as session:
        tools = await load_mcp_tools(session)
        yield session, tools
