# 0011. The agent investigation sees the claim as the web app sees it

- Status: Accepted
- Date: 2026-11-04

## Context

"Selidiki dengan agent" on a claim page starts `agent/investigate_worker.py` as a subprocess
(`web/reimburse_web/agent_bridge.py`). The worker opens the MCP server (`src/reimburse_buddy/server.py`) through
`agent/reimburse_agent/mcp_client.py`. That server is a third process with its own `DataStore`, and it reads
`data/*.json`. It never sees the web process's memory or the claim-state tables of ADR 0010.

Consequences today:

- **A claim an employee submitted on the web cannot be investigated.** `get_claim("CLM-2026-W001")` raises
  "Unknown claim" inside the server, so the run fails on the first tool call.
- **Forwarded files are invisible to the agent.** Web uploads live as `MSG-W…` messages in the web store's
  mailbox. `search_reimburse_mailbox` in the server only knows the seed mailbox.
- **Status changes are invisible too.** For a seed claim, the agent sees the seed status, not the status
  finance set on the web.

There is one safety rule the fix must keep. In the web app, an upload whose evidence could not be verified
(review reasons: unreadable file, self-declared document type, mismatch, duplicate, OCR disagreement) is
`pending` until a reviewer accepts it, and it is kept out of `check_claim` (`app.pending_items`,
`review_evidence`). A rejected upload never counts. If the agent saw those messages in the mailbox, it could
pass their IDs to `check_claim` and get a verdict that treats unverified files as evidence.

The seam already exists. `mcp_client.server_connection(extra_env=...)` passes environment variables to the
server, and `DataStore` reads `REIMBURSE_BUDDY_DATA` when it is set. The eval suite uses this to point the
server at fixture data.

## Decision

**Each investigation gets a read-only snapshot of the web's claim state, and its MCP server reads that
snapshot instead of `data/`.** The snapshot is taken when the run starts.

### 1. What the snapshot holds

A new function `snapshot_data_dir(cid) -> Path` in a new module `web/reimburse_web/agent_snapshot.py` writes a
fresh directory with the same file names `DataStore` loads:

| File | Content |
|---|---|
| `employees.json`, `policy.json`, `calendar.json`, `memos.json`, `replies.json` | copied unchanged from the seed directory |
| `claims.json` | `store.claims` as the web holds it now: seed claims with their current status, plus web submissions |
| `inbox.json` | `store.inbox` **minus** every message that `REVIEW` holds as `pending` or `rejected` for any claim |

- The filter in `inbox.json` is the safety rule from the Context. A message the reviewer has not accepted does
  not exist for the agent. An accepted message, or one that was clean on upload, is in the snapshot.
- Messages are written as the web holds them. `DataStore` normalises messages again on load, so the
  implementer must check that `normalize_message` gives the same result when applied twice. A test covers it.
- `outbox`, `approval_requests` and `status_log` are not files `DataStore` loads. They stay out. The agent's
  write tools start from an empty outbox, as today.

### 2. How the run uses it

- `Investigator.start(cid)` (`agent_bridge.py`) calls `snapshot_data_dir(cid)` before it spawns the worker. It
  passes the path with a new worker argument, `--data-dir <path>`.
- `investigate_worker.py` reads `--data-dir`. When set, it calls
  `mcp_tools(extra_env={"REIMBURSE_BUDDY_DATA": <path>})`. Without the argument, behaviour is exactly as today,
  so the eval suite and other callers do not change.
- The snapshot is fixed for the whole run. If finance accepts evidence while the agent runs, the agent does not
  see it until the next run. The investigation page says so in one line ("Agent membaca data klaim saat
  penyelidikan dimulai.").

### 3. Lifecycle and safety

- The directory is created under the system temp directory with a random name, readable only by the
  current user where the OS supports it (`tempfile.mkdtemp`).
- It is deleted when the run ends (`final`, `error`, cancel, or process exit), and by `stop_all` / Reset demo.
  A leftover directory from a crash is harmless: it holds synthetic data only. A startup hook in `app.py` removes
  snapshot directories older than a day. Snapshot directories share one name prefix, so the hook removes only
  those.
- No secret, DSN, uploaded file bytes or audit data goes into the snapshot. Only the JSON shapes above.
- If the snapshot cannot be written, the run is refused with an Indonesian message. The worker is not started
  on the seed data as a fallback, because that would silently investigate a different claim state.

### 4. What does not change

- The audit log, the approval gate, the budget reservation, rate limits and the run's events.
- `src/reimburse_buddy/` (server, `DataStore`, rules), `graph.py`, `plan_graph.py`.
- Masking on the employee side. This is a finance tool, and the snapshot never leaves the server host.
- For a seed claim with no web activity, the snapshot equals the seed. The demo hero claim gives the same
  result as before.

## Consequences

- A claim submitted on the web can be investigated end to end, using the files the reviewer has accepted.
- The agent and the claim page agree on the claim's items, status and verified evidence at the moment the run
  starts.
- Each run writes a few small JSON files. At demo scale this is negligible.
- Not included, each needs its own decision:
  - **Approved agent writes do not reach the web.** `send_message`, `request_missing_evidence`,
    `request_approval` and `update_claim_status` run inside the run's MCP server process and are lost when it
    exits. The employee portal and the claim page never show them. Mirroring approved writes into the web store
    and claim state (ADR 0010) is the next decision.
  - The reviewer's existing item decisions are not passed to the run. The agent can still ask the reviewer,
    and the answer enters through `reviewer_decisions` as today.
  - A live view of the web state during a run (an MCP server that reads the web process or Neon directly).

## Alternatives considered

- **Let the MCP server read the claim-state tables in Neon.** This is the cleanest long-term answer, but it puts
  a database dependency and the review-state filter into `src/reimburse_buddy/`, which version A (WorkBuddy
  over MCP) also runs. It also needs a DSN in the server process.
- **Pass the claim inline in the prompt.** The tools would still read the seed and contradict the prompt.
- **Copy the whole mailbox, and tell the agent in the prompt which messages are pending.** This relies on the
  model to follow an instruction for a safety rule. Filtering the data enforces it.

## Acceptance

- An employee submits a claim on the web with one clean upload and one upload that needs review. Finance starts
  an investigation on it with the fake script. The run's `get_claim` returns the web claim. Its
  `search_reimburse_mailbox` finds the clean upload and does not find the pending one.
- After finance accepts the pending upload, a new run finds it. After finance rejects it, a new run does not.
- A seed claim's investigation gives the same events as before (existing fake-script tests unchanged).
- The snapshot directory is gone after the run ends and after cancel.
- When the snapshot cannot be written, no worker is started and the page says why.
- `uv run pytest -q tests` and `uv --directory agent run pytest -q` are green, and CI is green on the PR.

## Notes for the implementer

- **Files in scope:**
  - `web/reimburse_web/agent_snapshot.py` (new);
  - `web/reimburse_web/agent_bridge.py` (start, cleanup, command);
  - `web/reimburse_web/app.py` (refusal message, one line on the investigation page, the startup hook);
  - `web/reimburse_web/labels.py`;
  - `agent/investigate_worker.py` (`--data-dir` only);
  - tests.
- **Do not touch:**
  - `src/reimburse_buddy/`;
  - `agent/reimburse_agent/graph.py`, `plan_graph.py`, `audit.py`, `mcp_client.py` (its `extra_env` is
    already enough);
  - migrations, `claim_state.py` write paths, masking.
- Tests must run without a model key (use `--fake-script`), without Tesseract and without a database.
- Never read or print `.env.local`, DSNs or keys.
