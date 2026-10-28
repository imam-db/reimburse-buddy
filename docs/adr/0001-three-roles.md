# 0001. Three roles: employee, manager, finance

- Status: Accepted
- Date: 2026-10-28

## Context

`app_users.role` accepts `employee`, `reviewer` and `finance` (`agent/migrations/007_app_users.sql`),
but the web app treats `reviewer` and `finance` the same (`REVIEWER_ROLES` in
`web/reimburse_web/users.py`): both open every admin page, including batch approval and demo reset.
The name `reviewer` is left over from the old shared-password demo gate.

The data already describes two different jobs. EMP-100 (Sales) and EMP-101 (Operations) are department
managers and the `manager_id` of their staff; EMP-102 is in Finance. `request_approval(manager_id, …)`
already asks the claimant's own manager and refuses anyone else, but nothing lets a manager answer.

## Decision

| Role (DB value) | Shown as | May open | Lands on |
|---|---|---|---|
| `employee` | Karyawan | `/employee/*` | `/employee` |
| `reviewer` | Manager | `/manager/*` | `/manager` |
| `finance` | Finance | `/admin/*`, `/` | `/admin` |

- The database value stays `reviewer`: no migration, no change to the `CHECK` constraint. Only the
  label and the routing change. Code uses named helpers (`is_manager`, `is_finance`) instead of
  comparing strings.
- Finance alone makes claim decisions: triage, evidence requests, item decisions, status changes,
  batch approval, agent investigations, request intake and plan approval.
- A manager only answers approval requests for their own team ([0003](0003-manager-approval-requests.md)).
- `POST /admin/reset` is finance-only and refused in production.
- The self-approval refusal stays for every role that can approve something.
- A wrong-role navigation is redirected to that role's landing page; any other method gets `403`.

## Consequences

- The README self-approval scene ("signed in as EMP-101 the approve controls are hidden") no longer
  works, because EMP-101 is a manager and cannot open finance pages. It is rewritten: a finance user
  who owns a claim, and a manager who tries to answer a request on their own claim.
- `REVIEWER_ROLES`, `is_reviewer_role`, `current_reviewer` and `reviewer_of` are renamed or split as
  their meaning narrows; `reviewer_of` keeps returning the signed-in employee_id.
- Renaming the DB value to `manager` later needs a migration; that is out of scope here.
