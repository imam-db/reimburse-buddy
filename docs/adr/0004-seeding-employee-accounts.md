# 0004. Seeding the employee accounts

- Status: Accepted
- Date: 2026-10-28

## Context

`seed_users.py` creates one account per run, with `--role` typed by hand and the password prompted
twice with `getpass`. Only the manager/finance accounts have been seeded so far; the staff in
`data/employees.json` (EMP-001…005) have no login, so the employee side cannot be tried with a real
session. `employees.json` has no role field, only `grade` and `department`.

## Decision

- Add `--all` to `seed_users.py`: one upsert per row of `data/employees.json`, role derived from the
  row: `department == "Finance"` and `grade == "manager"` → `finance`; other `grade == "manager"` →
  `reviewer`; everyone else → `employee`. `--employee/--role` keeps working for single accounts.
- `--dry-run` prints the employee_id, email and derived role for every row and writes nothing.
- Passwords are prompted with `getpass`, never passed as arguments, never printed. With `--all` the
  script asks once per account by default; `--same-password` asks once and uses it for every account
  (demo convenience, documented as such).
- Run manually with the OWNER role, as before. Production is seeded only with Imam's approval.

## Consequences

- Eight accounts can be created in one run, so demos and manual checks of every role use real logins.
- The role mapping lives in code; a person who changes job needs a re-run with `--employee/--role`.
- `--same-password` is weaker; it is meant for the demo database only.
