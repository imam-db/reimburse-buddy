-- Plan state for the request intake, version B. Run as the OWNER role (migrations only). The app connects
-- as reimburse_app, which can SELECT, INSERT and UPDATE these tables but never DELETE: a stored request, a
-- plan version, a decision and a mock-office row are never removed by the application.
--
-- Why these tables exist: the plan graph's checkpointer and this state are what let a request that is
-- waiting on a "who" question or on an approval survive a web restart (docs/request-intake.md). The
-- checkpoint tables are NOT here: langgraph owns their shape and creates them with
-- `python -m reimburse_agent.office.checkpoint_setup` (which then grants reimburse_app on them).
--
-- audit_events and llm_spend are not touched. The audit log stays append-only, the spend ledger stays
-- INSERT + SELECT.
--
-- One row per fact, never a blob of everything:
--   plan_requests   the intake form, so "Jalankan ulang" can replay it
--   plan_versions   every staged plan, one row per version (a re-plan adds a row, it does not overwrite)
--   plan_decisions  the reviewer's decision map for one version
--   mock_office_rows  what the mock tools produced, idempotent per action_id
--
-- Note on the number: docs/workbuddy-task-login-db.md (untracked) also plans a 006. That plan is not
-- applied and its number has to move; 006 belongs to the plan state.

CREATE TABLE IF NOT EXISTS plan_requests (
    run_id     TEXT PRIMARY KEY,
    request    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plan_versions (
    run_id     TEXT        NOT NULL,
    version    INTEGER     NOT NULL CHECK (version >= 1),
    plan       JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, version)
);

CREATE TABLE IF NOT EXISTS plan_decisions (
    run_id   TEXT    NOT NULL,
    version  INTEGER NOT NULL CHECK (version >= 1),
    decision JSONB   NOT NULL,
    PRIMARY KEY (run_id, version)
);

CREATE TABLE IF NOT EXISTS mock_office_rows (
    run_id    TEXT NOT NULL,
    store     TEXT NOT NULL CHECK (store IN ('outbox', 'calendar', 'tasks')),
    action_id TEXT NOT NULL,
    row       JSONB NOT NULL,
    UNIQUE (run_id, store, action_id)
);
CREATE INDEX IF NOT EXISTS mock_office_rows_run ON mock_office_rows (run_id, store);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reimburse_app') THEN
        CREATE ROLE reimburse_app LOGIN PASSWORD 'CHANGE_ME_OUT_OF_BAND';
    END IF;
END $$;

REVOKE ALL ON plan_requests, plan_versions, plan_decisions, mock_office_rows FROM PUBLIC;
REVOKE ALL ON plan_requests, plan_versions, plan_decisions, mock_office_rows FROM reimburse_app;
GRANT SELECT, INSERT, UPDATE ON plan_requests, plan_versions, plan_decisions, mock_office_rows TO reimburse_app;
-- No DELETE and no TRUNCATE: an approved plan is part of the record of what the office did. A run that
-- should not be approved any more is expired in the audit log (audit.sweep_expired), not deleted here.
