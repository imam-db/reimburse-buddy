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
- **Human gate (allowlist):** only read-only tools run freely (`list_claims`, `get_claim`,
  `get_policy`, `search_reimburse_mailbox`, `get_calendar`, `find_approval_memo`, `check_claim`,
  `triage_claims`, `draft_decision`, `get_outbox`). Every other tool, including any write tool added
  to the server later, fires a LangGraph `interrupt()` first. `reset_demo` is not offered to the
  agent at all.
- **send_message is always gated** (it carries free text the model wrote). Evidence requests use
  the server tool `request_missing_evidence(claim_id, item_id, gap_code, evidence_ids?,
  human_decisions?)`, registered only when `REIMBURSE_ENABLE_EVIDENCE_TOOL=1` (the agent's
  `mcp_client` sets it; WorkBuddy's server keeps exactly 15 tools). It builds the text from a fixed
  template (no amounts, no decision), sends only to the claim's owner, accepts only a gap that still
  persists given the evidence and decisions passed, and doesn't resend an open request. The agent
  lets it run without approval only when: the claim is the run's claim, the gap code is a pure
  document request (`receipt_missing`, `boarding_pass_missing`, `hotel_nights_mismatch`), the gap
  is in the run's latest `check_claim` result for that item, and fewer than 3 such requests ran in
  this run. Anything else waits for approval. A version-B-only addendum appended to the loaded
  `SKILL.md` tells the model to use it (`SKILL.md` itself is unchanged).
- **Approvals name their call:** the worker gives each interrupt its own `approval_id` (not the
  model's `tool_call_id`); an answer without the matching id (stale page, second tab, double click)
  is rejected and never applied to another call. The trace shows a decision only after the worker
  acknowledges it (`answer_applied`). The approval box also shows `check_claim`'s current verdict.
- **Fewer prompts:** unknown or hidden tool names return an error without asking; an identical
  repeat of a declined call is declined automatically; at most 10 approval requests per run. A declined call never reaches the server; the model gets `reviewer_declined` back.

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

- `tests/test_mcp_tools.py`: loads the 16 tools over MCP and checks the hero claim
  (`CLM-2026-0110`, no evidence → `needs_info`, approvable Rp4.840.000).
- `tests/test_compact.py`: compact mode keeps IDs, gap codes and money figures and shrinks outputs.
- `tests/test_graph_interrupt.py`: a scripted fake chat model proves the interrupt fires before
  each write tool, a declined write never reaches the server, and read tools / document requests
  run without a pause.

## Compact mode (agent side, default on)

`reimburse_agent/compact.py` shrinks tool outputs before the model sees them; the MCP server is
unchanged, so WorkBuddy is unaffected. It unwraps the adapter's text envelope (previously the JSON
was JSON-encoded a second time), minifies, drops null/empty fields and the per-finding
`key`/`params`/`resolved_by` (the `message` says the same), and trims mailbox snippets to 280
chars. IDs, statuses, gap codes and money figures are kept. `run.py --raw` / `eval --raw` turn it off.

| Measure (CLM-2026-0110) | Raw | Compact | Change |
|---|---|---|---|
| Bytes of 9 typical tool outputs (deterministic) | 17,815 | 10,682 | -40% (`check_claim` -62%) |
| Input tokens, one live run, deepseek-flash | 131,977 (18 calls) | 90,211 (16 calls) | -32% |
| Input tokens, mean of 3 eval runs, deepseek-flash | — | 95,332 | |

Single live runs are noisy (the model chose 18 vs 16 tool calls), so read the byte row as the
reliable number. Most of the remaining input is the SKILL.md system prompt resent on every
turn; DeepSeek serves ~90% of it from its prompt cache.

## Mini-eval (`eval/`)

```bash
cd agent
export DEEPSEEK_API_KEY=...   # never commit
uv run python -m eval.run_eval --model deepseek-flash --runs 3 --budget 0.25
uv run python -m eval.run_eval --model deepseek-v4-pro --runs 3 --budget 0.6
```

- **Claims** (`eval/cases.py`): CLM-2026-0110 (hero), 0104 (late check-out + duplicate taxi +
  out-of-trip item), 0101 (boarding pass; the other trip's pass is a trap), 0103 (hotel over cap,
  no memo: needs the reviewer), 0111 (clean: approved by triage with no gaps).
- **Expected outcomes** come from the rule engine itself (`src/reimburse_buddy/rules.check_claim`),
  called directly with the evidence a careful reviewer finds; no number is hand-typed.
- **Scoring** replays the agent's last `draft_decision` arguments (plus any `send_message` it
  sent, so scripted replies exist) through the engine on a fresh store and compares per-item status.
- **Scripted reviewer:** answers a decision question with `{"IT-3": "approve_capped"}` (0110) or
  `{"IT-1": "approve_capped"}` (0103), otherwise "no extra decision"; declines every gated write
  (no state changes in eval); at most 2 reviewer turns.
- **Gates:** ≥95% schema-valid tool calls (JSON-schema check of args + no shape errors from the
  server) and zero invented Rp figures (every `Rp…` in the final answer must appear in some tool
  output; `Rp0` excluded).
- **Cost:** DeepSeek's published prices, https://api-docs.deepseek.com/quick_start/pricing (read
  2026-10-24), billed at **peak** rates as an upper bound (off-peak is half). Cache hits are priced
  separately using `prompt_cache_hit_tokens` from the API.

### Results — 2026-10-24 (23:34–23:43 WITA), 5 claims × 3 runs, compact mode, temperature 0

| Metric | deepseek-flash (V4.1-Flash-1017) | deepseek-v4-pro (V4-Pro-0813) |
|---|---|---|
| Gate: schema-valid tool calls | **100%** (182/182) | **100%** (151/151) |
| Gate: invented Rp figures | **0** | **0** |
| Per-item decision accuracy | 100% (75/75) | 100% (75/75) |
| Exact approvable amount match | 15/15 | 15/15 |
| Wrong evidence cited as support in the note | 0 | 0 |
| Trap IDs passed to the engine (engine ignored them) | 9 | 8 |
| Unnecessary reviewer questions | 0 | 0 |
| Claims identical and correct in all 3 runs | 5/5 | 5/5 |
| Input tokens per claim (cache hit) | 58,206 (90%) | 55,268 (89%) |
| Output tokens per claim | 3,561 | 4,021 |
| Cost per claim (peak, upper bound) | $0.0063 | $0.026 |
| Latency p50 / p95 per claim | 26.7 s / 37.1 s | 39.7 s / 71.2 s |

Raw per-run data: `eval/results/*.json`. Total spend for all runs on this page (including one
discarded run from a harness bug) ≈ $0.60 at peak rates, ≈ $0.30 at the off-peak rate that
actually applied (Saturday).

**Pick:** deepseek-flash. Both pass every gate with identical quality on this set; flash is ~4×
cheaper and ~1.5× faster.

### Honest caveats

- **The set is too easy to separate models.** Both scored 100% on 5 synthetic demo claims whose
  data was written alongside the rules. This shows the agent + tools work end to end; it says
  nothing about real claims. Harder cases (ambiguous emails, conflicting sources, prompts in
  Indonesian only, larger mailboxes) are needed before any accuracy claim.
- **The engine does the money.** Amount accuracy mostly measures whether the agent collects the
  right evidence IDs and passes the reviewer's decision through; the totals are computed by
  `check_claim`, not the model.
- **"Trap IDs passed" is not a failure:** the models pass the unsigned memo / other trip's pass to
  `check_claim`, which lists them under `ignored_evidence`, and the note says so. The "cited as
  support" check is a text heuristic (trap ID mentioned with no rejection wording within 150
  chars); it was tuned on these outputs after two false positives.
- **Reviewer questions** are counted when the model stops without a finished note; the script
  answers instantly and never pushes back, so real reviewer interaction is not tested.
- **Gated writes:** no run tried a gated write (0 declined); runs often end by *offering* status
  updates. Follow-up `send_message` asking an employee for a receipt runs ungated by design.
- n = 3 runs per claim at temperature 0; p95 over 15 runs is close to the max.

## Next

Langfuse tracing, harder eval cases, the "Investigate" button in `web/`, Qwen / Gemini Flash
comparison.

## Investigate button (web admin)

`web/` claim pages have an **Investigate with agent** button. The web app starts
`agent/investigate_worker.py` as a subprocess (`uv --directory agent run --frozen --no-dev python investigate_worker.py --claim ...`)
and talks to it over JSON lines (trace steps, interrupt, final note). Every non-read-only call pauses on
the LangGraph interrupt and runs only after the reviewer clicks **Approve**; **Decline** resumes with
`reviewer_declined`. The web side checks and writes each answer under one lock, sends the
`tool_call_id`, and ignores duplicates.

Run limits: `REIMBURSE_AGENT_RUN_DEADLINE` (default 600 s of agent work; the clock pauses while an
approval is pending), `REIMBURSE_AGENT_APPROVAL_DEADLINE`
(default 900 s, then the pending call is auto-declined), model calls time out after 60 s with one retry.
**Cancel run** stops a run; each run is its own process group, so uv, python and the MCP server all
exit. stderr goes to a per-run log (`REIMBURSE_AGENT_LOG_DIR`, default a temp dir); on errors the page
shows the last 20 lines with keys redacted. Web tests that spawn the agent are opt-in:
`uv run pytest -m agent` (or `RB_AGENT_TESTS=1`).

Why a subprocess: importing the agent into `web/` would pull langgraph/langchain-mcp-adapters (and their
`mcp` pin) into the root `uv.lock` the hackathon MCP server runs from. The subprocess keeps `agent/` on its
own lockfile; the JSON-lines protocol is the only contract.

Config (web server env): `DEEPSEEK_API_KEY` or `REIMBURSE_AGENT_API_KEY` (without it the page shows a
"no key configured" message and runs nothing), `REIMBURSE_AGENT_MODEL` (default `deepseek-flash`),
`REIMBURSE_INVESTIGATE_MAX_PER_MIN` (default 3; one live run at a time), `REIMBURSE_AGENT_MAX_TOKENS`
(default 250000 per run). Compact tool outputs are always on.

Known limit: the agent runs against its own MCP server process (fresh demo state), so approved writes land
in that session, not in the web page's in-memory store. A shared database fixes this later.
