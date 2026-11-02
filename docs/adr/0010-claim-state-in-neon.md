# 0010. Claim state survives a restart: web writes go to Neon

- Status: Accepted
- Date: 2026-11-02

## Context

Neon already holds the audit log (001–004), the LLM spend ledger (005), plan state and checkpoints (006) and
the user accounts (007). Everything a person does to a claim in the web app does not. It lives in the web
process and is gone after a restart or a redeploy:

| State | Where it lives today | Written by |
|---|---|---|
| Claims submitted by employees (batch `web-submissions`) | `store.claims` (list in memory) | `upload_flow._submit`, `app.py` (older upload path) |
| Forwarded files as mailbox messages (`MSG-W…`), incl. `evidence_status` | `store.inbox` | `upload_flow._forward`, `app.py`, `review_evidence` |
| Reviewer working state per claim: accepted evidence, item decisions, pending evidence, evidence log | `app.REVIEW` | `add_evidence`, `review_evidence`, `decide`, `_forward` |
| Employee drafts | `app.DRAFTS` | `upload_flow` (`/employee/drafts…`) |
| Messages to employees (simulated outbox) and delivered scripted replies | `store.outbox`, `store.inbox`, `replies[*]._delivered` | `DataStore.send_message` via `/admin/claims/{cid}/message` |
| Approval requests to managers and their answers | `store.approval_requests` | `DataStore.request_approval`, `manager.answer` |
| Claim status changes | `claim["status"]`, `store.status_log` | `DataStore.update_claim_status` |
| Uploaded files | `web/uploads/` on local disk | `upload_flow` |

Consequences seen today:

- A restart erases every claim an employee submitted. The audit log in Neon still has the manager answers and
  plan events that point at those claims, so the two disagree.
- The audit log records a manager's answer, but the request it answered disappears on restart.
- On a host with an ephemeral disk, uploaded files disappear too, even where a claim survives.
- README "Known limits" already lists this ("shared in-memory state … a restart loses them").

The seed data (`data/*.json`: employees, policy, the demo claims, the seed mailbox, calendar, memos, scripted
replies) is synthetic, read-only and versioned in git. It does not need a database.

## Decision

**Seed stays in JSON; everything the web app writes goes to Neon.** On startup the web process loads the
seed as today, then replays the stored rows on top of it. Every write goes to the database first and then to
memory. Reads keep using memory, so the pages, `check_claim`, triage and masking code are unchanged.

### 1. One switch, one pool

- The claim state uses the existing switch: with `REIMBURSE_PLAN_BACKEND=postgres` (the production default)
  claim state lives in Neon too. With `file` (the default outside production) it stays in memory as today,
  so the test suite and local runs behave exactly as before.
- It uses the same DSN (`REIMBURSE_STATE_DATABASE_URL`, falling back to `REIMBURSE_AUDIT_DATABASE_URL`) and the
  same pool as `plan_state.pool()`. No new environment variable.
- `startup_check` fails startup when the backend is `postgres` and any table of migration 008 is missing or
  unusable, with a message naming the migration (same style as `plan_state.startup_check`). There is no
  silent fallback to memory.

### 2. Migration `agent/migrations/008_claim_state.sql`

Same rules as 006: idempotent, run by the owner role, the app role `reimburse_app` gets
`SELECT, INSERT, UPDATE` and **never `DELETE` or `TRUNCATE`**. One row per fact; JSONB holds the same dict
shape the web code already uses, so no mapping layer is needed.

| Table | Key | Holds |
|---|---|---|
| `demo_epoch` | single row | the current epoch (see 4) |
| `web_claims` | `(epoch, claim_id)` | a submitted claim, same shape as a `claims.json` entry; `employee_id` column |
| `web_messages` | `(epoch, message_id)` | a mailbox message added by the web app (uploads, delivered replies); `owner_id`, `claim_id` columns. Updated when `evidence_status` changes |
| `claim_reviews` | `(epoch, claim_id)` | the `REVIEW[cid]` dict (evidence, decisions, pending, evidence_log, origin) |
| `employee_drafts` | `(epoch, draft_id)` | a draft; `employee_id` column; `submitted_at` set on submit instead of deleting |
| `outbox_messages` | `(epoch, message_id)` | an outbox message |
| `approval_requests` | `(epoch, request_id)` | a request; updated when a manager answers |
| `claim_status_changes` | identity | one status change (`claim_id`, from, to, note), replayed in order |
| `uploaded_files` | `(epoch, stored_as)` | the file bytes (`BYTEA`), `owner_id`, `content_type`, `size_bytes`, `sha256` |

Claim and message IDs keep their format (`CLM-2026-W001`, `MSG-W001`, `OUT-001`, `APR-001`). They are still
derived from the count, but the insert relies on the primary key: on a conflict the writer takes the next
number and retries, so two requests can never get the same ID.

### 3. Write path

- A new module `web/reimburse_web/claim_state.py` owns the tables: a `ClaimRows` protocol with
  `MemoryClaimRows` (no-op, the default) and `PostgresClaimRows(pool)`. It exposes one function per fact
  (`add_claim`, `add_message`, `update_message`, `save_review`, `save_draft`, `mark_draft_submitted`,
  `add_outbox`, `save_approval_request`, `add_status_change`, `save_file`, `read_file`) and `hydrate(store,
  review, drafts)`.
- **Database first, then memory.** If the write raises, the route answers 503 with an Indonesian message and
  memory is not changed. A write the database did not keep must not look kept.
- **One transaction per user action.** Submitting a claim writes the claim, its forwarded messages, its
  review state and the draft's `submitted_at` together, or none of them.
- Calls into `DataStore` (`send_message`, `request_approval`, `update_claim_status`) stay as they are. The web
  wrapper persists what the call added (the new outbox row, delivered replies, the new request, the status
  change). `src/reimburse_buddy/` is not edited.
- The manager answer keeps its order (ADR 0003): audit log first, then the request row, then memory.

### 4. Startup, restart and Reset demo

- `hydrate` runs on startup after `plan_state.startup_check`. Starting from the seed, it adds `web_claims`
  and `web_messages`, replays `claim_status_changes` in order, and fills `REVIEW`, `DRAFTS` (unsubmitted
  only), `outbox` and `approval_requests`. A scripted reply counts as delivered when its reply message is in
  `web_messages`.
- **Reset demo** cannot delete rows. With the postgres backend it increments `demo_epoch`, then reloads, so
  every table reads as empty for the new epoch while old rows stay in place. Reset stays refused in
  production (unchanged).
- Only one web process is supported, as today. Hydrating once at startup does not make two instances agree;
  that needs reads from the database on each request and is not part of this decision.

### 5. Uploaded files

- Uploads are still written to `UPLOAD_DIR`, because extraction, OCR and duplicate detection read from disk.
  With the postgres backend the bytes are also stored in `uploaded_files`, in the same transaction as the
  draft or message that references them.
- Every route that serves a file (`/employee/drafts/{did}/files/{fid}`, `/admin/claims/{cid}/files/{mid}`
  from ADR 0009) gets its path from one helper. That helper restores a missing file from the database into
  `UPLOAD_DIR` before serving it. The existing path, ownership and content-type checks are unchanged.
- The 5 MB limit per file stays. At demo scale `BYTEA` is acceptable; object storage is a later decision.

## Consequences

- A restart or redeploy keeps submitted claims, reviewer decisions, drafts, messages, manager requests and
  files. Triage numbers for the seed batch (10 / 7 / 3) do not change, because the seed is untouched.
- The audit log and the claim state stay consistent: a request the audit log answered still exists.
- Each user action becomes a database round trip, and a Neon endpoint waking up adds latency to the first
  one. The pool's `check_connection` already handles suspended endpoints.
- Old epochs accumulate rows. At demo volume that is harmless; cleanup is an owner-role task, never the app.
- Not included, each needs its own decision:
  - **The agent investigation still cannot see web claims.** The worker's MCP server reads `data/*.json` in
    its own process. A likely fix is to give the run a snapshot directory through
    `REIMBURSE_BUDDY_DATA` (`mcp_client` already accepts `extra_env`), but that touches
    `investigate_worker.py`.
  - more than one web instance;
  - persisting the Q&A history (`qa.STATE`) and the running investigations;
  - object storage for files.

## Alternatives considered

- **Move all data, seed included, into Postgres and make `DataStore` read the database.** That is the clean end
  state, but it changes `src/reimburse_buddy/`, which the MCP server (version A) and the agent also use, and
  it moves synthetic fixtures out of git. The overlay keeps both untouched.
- **Write the in-memory state to JSON files on disk.** Simple, but it does not survive a host with an
  ephemeral disk, and it adds a second store next to Neon. The project already keeps state in Neon.
- **Read from the database on every request instead of hydrating.** This is needed for several instances, but
  it means rewriting every page that iterates `store.claims` or `REVIEW`. The decision above keeps the pages
  as they are, and the multi-instance case is listed as not included.
- **Neon Object Storage for files.** It fits better for large files and branches with the database, but it adds
  a service and credentials. `BYTEA` is enough for 5 MB synthetic receipts.

## Acceptance

- With `REIMBURSE_PLAN_BACKEND=postgres` against a Neon branch with 006–008 applied: an employee submits a
  claim with files, finance accepts one piece of evidence and decides one item, sends a message and asks a
  manager. After the web process restarts, the claim page, triage, the employee portal, the Q&A answer, the
  manager page and both file routes show the same state as before the restart.
- Reset demo (outside production) empties the web state, and the seed numbers stay 10 / 7 / 3.
- When a database write fails, the route answers 503 and memory is unchanged (test with a failing
  `ClaimRows`).
- Without `REIMBURSE_PLAN_BACKEND=postgres`, behaviour and every existing test are unchanged.
- The app role cannot `DELETE` from any table of 008.
- `uv run pytest -q tests` and `cd agent && uv run pytest -q` are green; tests marked `postgres` cover the
  restart scenario and are skipped without `DATABASE_URL`.

## Notes for the implementer

- Depends on ADR 0009 being merged into `main`. Branch from `main` after that merge.
- **Files in scope:** `web/reimburse_web/claim_state.py` (new), `app.py`, `upload_flow.py`, `manager.py`,
  `plan_state.py` (pool reuse and startup order only), `labels.py`, `agent/migrations/008_claim_state.sql`
  (new), README (env table, migration steps, "Known limits"), and tests.
- **Do not touch:** `src/reimburse_buddy/`, `agent/reimburse_agent/graph.py`, `plan_graph.py`, `audit.py`,
  migrations 001–007, the masking functions, `evidence_review_reasons`.
- **Never** apply a migration to the production Neon branch; write the SQL and the steps. Never read or print
  `.env.local`, DSNs or passwords.
