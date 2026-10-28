# Architecture decision records

One file per decision, numbered in order. A record is written before the work starts and stays in the
repo after it lands; a later decision that changes it adds a new record and marks the old one
`Superseded by NNNN`.

Status: `Proposed` (written, not yet agreed) → `Accepted` (agreed, work may start) → `Superseded` /
`Rejected`.

| # | Title | Status |
|---|---|---|
| [0001](0001-three-roles.md) | Three roles: employee, manager, finance | Accepted |
| [0002](0002-employee-identity-from-session.md) | Employee identity comes from the login session | Accepted |
| [0003](0003-manager-approval-requests.md) | Managers answer approval requests; finance decides | Accepted |
| [0004](0004-seeding-employee-accounts.md) | Seeding the employee accounts | Accepted |

## Template

```markdown
# NNNN. Title

- Status: Proposed
- Date: YYYY-MM-DD

## Context
What is true today and why it needs a decision.

## Decision
What we will do, stated so a reviewer can check the code against it.

## Consequences
What gets easier, what gets harder, what is left out.
```
