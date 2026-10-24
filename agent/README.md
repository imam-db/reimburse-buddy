# reimburse-agent (version B, work in progress)

> **DO NOT MERGE before Oct 31.** Version A (WorkBuddy skill + MCP server) is frozen for the
> hackathon. This package lives entirely under `agent/` with its own `pyproject.toml` and
> `uv.lock`, so it cannot change version A's environment.

A LangGraph agent that drives the **same** `reimburse-buddy` MCP server WorkBuddy uses, with any
OpenAI-compatible tool-calling model (DeepSeek, Qwen, Gemini via its OpenAI endpoint, ...).

- **Server:** started over stdio with `uv --directory .. run --no-dev reimburse-buddy-mcp`
  (one fresh process per session = clean demo state). Note: `--no-dev` syncs the root `.venv`
  without dev packages; the next root `uv run pytest` reinstalls them.
- **System prompt:** `skills/claim-auditor/SKILL.md`, loaded as-is.
- **Human gate:** a LangGraph `interrupt()` fires before `update_claim_status`,
  `approve_cleared_batch`, `request_approval`, and any `send_message` that communicates a
  decision (approve/reject wording, EN + ID). Asking an employee for a missing document is allowed.
  A declined call never reaches the server; the model gets `reviewer_declined` back.

## Run

```bash
cd agent
cp /dev/null .env   # optional; .env is git-ignored. Never commit keys.
export REIMBURSE_AGENT_MODEL=deepseek-flash
export REIMBURSE_AGENT_BASE_URL=https://api.deepseek.com
export REIMBURSE_AGENT_API_KEY=...        # your key
uv run python run.py --claim CLM-2026-0110          # asks y/N at each gated call
uv run python run.py --claim CLM-2026-0110 --auto-decline   # scripted: decline every gated call
```

## Test (no API key, no network LLM)

```bash
cd agent && uv run pytest
```

- `tests/test_mcp_tools.py`: loads the 15 tools over MCP and checks the hero claim
  (`CLM-2026-0110`, no evidence → `needs_info`, approvable Rp4.840.000).
- `tests/test_graph_interrupt.py`: a scripted fake chat model proves the interrupt fires before
  each write tool, a declined write never reaches the server, and read tools / document requests
  run without a pause.

## Next (after Oct 31)

Langfuse tracing, the 5-claim mini-eval (0110, 0104, 0101, 0103, one clean claim; 3 runs each;
gates: ≥95% schema-valid tool calls and zero invented Rp figures), model comparison.
