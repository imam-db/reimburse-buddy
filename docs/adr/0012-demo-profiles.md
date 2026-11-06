# 0012. "Pilih profil demo": one-click sign-in in the demo environment only

- Status: Accepted
- Date: 2026-11-06

## Context

The demo (`docs/demo-script.md`) switches between two people: Rina (employee) submits a claim, and Rani (finance)
decides it. Today each switch is a sign-out, an email and a password. In a recorded demo that costs time and
shows password typing on screen. When grokbot drives the browser, it either needs the passwords (which must not
be passed through chat or stored in the repo) or a person has to sign in for it at every switch.

Other demos in the same competition open on a profile picker: cards with a name, a job title and a role, one
click to enter. It explains the roles before anyone reads a menu.

The app already has everything a picker needs except the shortcut itself:

- accounts live in `app_users` (migration 007), with the role (`employee`, `reviewer` shown as "Manager",
  `finance`) and `is_active`;
- the session is the signed `rb_auth` cookie from `auth.new_cookie_value(employee_id, role)`;
- `auth.home_for(role)` and `auth.may_open(role, path)` decide where each role lands and what it may open;
- production is `APP_ENV=prod`, where `auth.startup_check` already refuses unsafe configurations.

A picker signs someone in without a password. That is acceptable only in an environment that holds synthetic
data and is not reachable as production. So the decision is mostly about where the picker must never appear.

## Decision

**A demo-only switch, `REIMBURSE_DEMO_PROFILES=1`, turns `/login` into a profile picker. The cards are real
accounts from `app_users`, and a click signs in as that account. Production refuses to start with the switch on.**

### 1. When the picker exists

- It is on only when all of these hold:
  - `REIMBURSE_DEMO_PROFILES=1`;
  - not production (`auth.is_production()` is false);
  - the login gate is on (`auth.enabled()`).

  With the gate off there is no sign-in at all, as today.
- `auth.startup_check` raises `AuthConfigError` when `REIMBURSE_DEMO_PROFILES` is set (any non-empty value) and
  `APP_ENV` is production. A typo in a deploy can never open passwordless sign-in.
- One helper, `auth.demo_profiles_enabled()`, answers the question everywhere. Pages and routes do not read the
  variable themselves.

### 2. Which profiles

- A fixed, ordered list in code: `DEMO_PROFILES = ("EMP-001", "EMP-003", "EMP-100", "EMP-101", "EMP-102")`. That
  is two employees, two managers and finance, all from `data/employees.json`.
- A card is shown only when that employee has an **active** row in `app_users`. Its role comes from the row, not
  from the list or the request, so the picker cannot invent a role.
- This needs one read in the user store: `UserStore.find_by_employee_id(employee_id) -> User | None`, in
  `MemoryUserStore` and `PostgresUserStore`. The app role already has `SELECT` on `app_users`.
- Each card shows:
  - initials in a circle, not a photo (no stock photos of real-looking people);
  - the name and department from `employees.json`;
  - the role chip in Indonesian (`users.ROLE_LABELS`);
  - one sentence on what that role does in the demo, from `labels.py`, for example "Kirim klaim dan unggah struk"
    for an employee or "Triage, tinjau bukti, dan putuskan klaim" for finance.

### 3. Signing in through a card

- Each card is a small form: `POST /login/demo` with `employee_id` and `next`. It works without JavaScript.
- The route:
  - answers 404 unless `auth.demo_profiles_enabled()`;
  - accepts only an `employee_id` that is in `DEMO_PROFILES` **and** has an active `app_users` row, and answers
    404 otherwise (the same answer, so the route does not reveal which accounts exist);
  - sets the cookie exactly as `login_submit` does (`auth.new_cookie_value(user.employee_id, user.role)`, the
    same flags and TTL);
  - redirects to `next` when that role may open it, else to `auth.home_for(role)`;
  - writes one line to the server log per sign-in: `demo profile sign-in: <employee_id> (<role>)`, without the IP.
    The audit log is not used: its event types are fixed by migrations 001–004 and `audit.py`, and a password
    sign-in is not audited either. Every action after sign-in is audited as before, under the same identity.
- Self-approval, role checks and masking are unchanged, because they read the session, which is the same cookie.

### 4. On the page

- `/login` with the picker on shows:
  - the title "Pilih profil" and the line "Mode demo: masuk tanpa kata sandi. Data sintetis.";
  - the cards in a responsive grid (one column at 375 px);
  - a legend of what each role may open.

  The email and password form stays, under a closed `<details>` "Masuk dengan email".
- In the header, a signed-in user sees **"Ganti profil"** in place of the "Keluar" label. It is the same
  `POST /logout`, which already lands on `/login`, so the picker opens in one step.
- Without the switch, `/login` and the header look exactly as today.

## Consequences

- Switching roles in the demo is one click. grokbot never needs a password, and the recording never shows one.
  The account rows still come from a person running `seed_users` once per demo database.
- Anyone who can reach a server running with the switch can act as any of the five accounts. The demo
  environment must therefore hold synthetic data only, run on a demo database (a Neon branch of its own, not
  `production`), and not be exposed publicly beyond the demo. The startup refusal covers production, and
  operator discipline covers the rest.
- Not included:
  - photos or custom avatars;
  - a picker for every account in `employees.json`;
  - a public read-only demo without accounts.

## Alternatives considered

- **Pass passwords to the bot through a password manager.** This works for grokbot but not for a fast recorded
  role switch, and it still shows a login form at every switch.
- **Use the dev-only `REIMBURSE_DEV_EMPLOYEE` with the gate off.** That is one fixed employee and no finance
  sign-in, so it cannot show the two-role story.
- **Make the picker a production feature with a role check.** It is still passwordless sign-in, and there is no
  case for it outside a demo.

## Acceptance

- With `REIMBURSE_DEMO_PROFILES=1`, not production, and the five accounts seeded:
  - `/login` shows five cards with the right names, departments and role chips;
  - clicking Rina lands on `/employee`, Dewi on `/manager`, and Rani on `/admin`;
  - "Ganti profil" returns to the picker.
- A profile without an active `app_users` row is not shown, and `POST /login/demo` for it answers 404. So does
  an `employee_id` outside `DEMO_PROFILES`, and every request when the switch is off.
- `APP_ENV=prod` with `REIMBURSE_DEMO_PROFILES=1` refuses to start, with a message that names the variable.
- Each demo sign-in writes one server log line with the employee ID and role, and no IP.
- Without the switch, the existing login tests pass unchanged.
- `uv run pytest -q tests` and `uv --directory agent run pytest -q` are green, and CI is green on the PR.

## Notes for the implementer

- **Files in scope:**
  - `web/reimburse_web/auth.py` (`demo_profiles_enabled`, the startup check);
  - `web/reimburse_web/users.py` (`find_by_employee_id`);
  - `web/reimburse_web/app.py` (`/login` page, `POST /login/demo`, the header link);
  - `web/reimburse_web/labels.py`;
  - `web/reimburse_web/static/app.css`;
  - README (env table, a short "Demo environment" note);
  - tests.
- **Do not touch:**
  - migrations;
  - `seed_users.py` behaviour;
  - password hashing;
  - `may_open` / `home_for`;
  - masking;
  - `src/reimburse_buddy/` and the agent.
- Tests use `MemoryUserStore`. They need no database, no Tesseract and no model key.
- Never read or print `.env.local`, DSNs, passwords or keys.
