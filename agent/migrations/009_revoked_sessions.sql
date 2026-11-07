-- Logout that survives a restart (security audit follow-up to PR #66). Run as the OWNER role (migrations only),
-- on both Neon branches (app and demo), in the same database as 007_app_users.sql.
--
-- The session cookie is stateless: `issued|employee_id|role|nonce` plus an HMAC (web/reimburse_web/auth.py).
-- POST /logout records the cookie's nonce here until the cookie would have expired anyway, and every gated
-- request checks it in the same query that re-reads the user row. Without this table the app falls back to an
-- in-process list that is lost on restart (it logs that once at startup and keeps working).
--
-- The nonce alone is not a credential: a cookie also needs the HMAC made with REIMBURSE_DEMO_SESSION_KEY.
-- The app connects as reimburse_app, which may add a revocation, read them, and delete expired ones.
--
-- Run (owner DSN, never committed):
--   psql "$OWNER_DATABASE_URL" -f agent/migrations/009_revoked_sessions.sql

CREATE TABLE IF NOT EXISTS revoked_sessions (
    nonce       TEXT        PRIMARY KEY,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS revoked_sessions_expires_at ON revoked_sessions (expires_at);

GRANT SELECT, INSERT, DELETE ON revoked_sessions TO reimburse_app;
