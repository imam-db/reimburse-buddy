# 0003. Managers answer approval requests; finance decides

- Status: Accepted
- Date: 2026-10-28

## Context

`DataStore.request_approval(manager_id, claim_id, item_id, reason)` (`src/reimburse_buddy/data_store.py`)
creates a pending request for the claimant's manager and refuses any other manager. The agent graph can
stage it through the approval gate, but no page lets the manager answer, and nothing reads the answer.

## Decision

- New page `/manager` for role `reviewer` (shown as Manager): the pending requests addressed to the
  signed-in manager, each with the claim, item, amount, reason, and Setujui / Tolak plus a short note.
- A manager may answer only a request whose `manager_id` is their own `employee_id`, and never one on
  their own claim. Anything else is `404` for reads and refused for writes.
- An answer sets the request's `status` (`approved` / `rejected`), `answered_by`, `answered_at` and
  `note`, once; a second answer is refused. Each answer, and each refusal, is written to the audit log.
- The finance claim page shows the manager's answer next to the item. It is input for finance, not a
  decision: the rule engine (`check_claim`) and the approval gate are not changed, and finance still
  makes the item decision and the status change.

## Consequences

- Protected code stays untouched: `rules.py`, `plan_graph` gate, `graph.py`, `audit.py`, migrations.
  New audit event types, if needed, are a separate decision.
- The answer lives in the in-memory store like the rest of the demo data, so a restart or demo reset
  clears it.
- Making a manager approval close a gap automatically (as a signed memo does) would change the rule
  engine and needs its own ADR.
