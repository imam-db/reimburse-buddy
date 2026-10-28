-- Login accounts for the version B web app. Run as the OWNER role (migrations only). The app connects as
-- reimburse_app, which can SELECT this table and UPDATE only last_login_at: it can never INSERT or DELETE a
-- user or change a password hash. Accounts are created or rotated by the owner with
-- `python -m reimburse_web.seed_users` (web/reimburse_web/seed_users.py); no password lives in the repo.
--
-- employee_id matches data/employees.json (EMP-xxx). email is stored lowercase. password_hash is
-- `scrypt$n$r$p$<salt_hex>$<hash_hex>` (web/reimburse_web/users.py).
--
-- Note on the number: docs/workbuddy-task-login-db.md asked for 006, but 006_plan_state.sql already took it.
--
-- Run (owner DSN, never committed):
--   psql "$OWNER_DATABASE_URL" -f agent/migrations/007_app_users.sql

CREATE TABLE IF NOT EXISTS app_users (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    employee_id    TEXT        NOT NULL UNIQUE,
    email          TEXT        NOT NULL UNIQUE,
    password_hash  TEXT        NOT NULL,
    role           TEXT        NOT NULL CHECK (role IN ('employee', 'reviewer', 'finance')),
    is_active      BOOLEAN     NOT NULL DEFAULT TRUE,
    last_login_at  TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT ON app_users TO reimburse_app;
GRANT UPDATE (last_login_at) ON app_users TO reimburse_app;
