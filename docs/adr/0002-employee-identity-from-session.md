# 0002. Employee identity comes from the login session

- Status: Accepted
- Date: 2026-10-28

## Context

Login now signs a cookie with the user's `employee_id` and `role`, but the employee pages still take
the employee from the request:

- `GET /employee?emp=` with a "Signed in as (simulated)" dropdown, `GET /employee/new?emp=`.
- `emp` as a form field on `POST /employee/claims`, `POST /employee/forward`, `POST /employee/drafts`.
- `GET/POST /employee/drafts/{did}` and `POST /employee/claims/{cid}/items/{iid}/files` do not check
  the owner; `GET /employee/claims/{cid}` checks it only when `EMPLOYEE_QA=1`.

A signed-in employee can therefore read and act on another employee's claims.

There are also two "my claims" pages: `/employee` (full portal, picker) and `/employee/claims` (bare
list, where login sends employees).

## Decision

- One helper, `current_employee(request) -> str | None`, is the only source of the acting employee:
  the session's `employee_id` when the gate is on; `REIMBURSE_DEV_EMPLOYEE` only when the gate is off
  (the same rule `qa.py` already uses). No route reads an employee ID from a query string or form.
- Every employee route checks ownership of the claim, draft or item it touches, regardless of
  `EMPLOYEE_QA`. Someone else's object answers `404`, exactly like a missing one.
- A draft records its owner from the session when it is created.
- `/employee` is the single portal (my claims, messages from finance, new claim, forwarded files).
  `/employee/claims` redirects to it; `auth.home_for("employee")` points to it.

## Consequences

- Tests that drive employee pages with `?emp=` move to signing in (or setting `REIMBURSE_DEV_EMPLOYEE`).
- New tests: employee A cannot see or change B's claim, draft or upload, including with a forged `emp`
  field or query.
- Finance pages keep showing any employee's claims; this record only covers `/employee/*`.
