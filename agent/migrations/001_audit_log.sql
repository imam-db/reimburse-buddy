-- Audit log, version B. Run as the OWNER role (migrations only). The app connects as reimburse_app,
-- which can INSERT and SELECT but never UPDATE or DELETE: the log is append-only for the application.
-- Replace the password placeholder out of band; never commit a real one.

CREATE TABLE IF NOT EXISTS audit_events (
    seq        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- insertion order
    id         UUID        NOT NULL UNIQUE,
    event_key  TEXT        NOT NULL UNIQUE,                      -- run_id:event_type:ref, dedupes resumes
    run_id     TEXT        NOT NULL,
    action_id  TEXT,
    ts         TIMESTAMPTZ NOT NULL,
    actor      TEXT        NOT NULL CHECK (actor ~ '^(agent|system|human:[^[:space:]:][^:]{0,79})$'),
    event_type TEXT        NOT NULL CHECK (event_type IN ('request', 'plan', 'tool_call', 'staged_action',
                   'approval', 'rejection', 'execution', 'clarification', 'limit', 'refusal', 'access_denied',
                   'injection_notice', 'expired', 'error')),
    args_hash  TEXT,
    payload    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    sources    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    CHECK (event_type NOT IN ('staged_action', 'approval', 'rejection', 'execution', 'expired') OR action_id IS NOT NULL),
    CHECK (event_type NOT IN ('staged_action', 'approval', 'execution') OR args_hash IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS audit_events_run ON audit_events (run_id, seq);
CREATE INDEX IF NOT EXISTS audit_events_action ON audit_events (action_id, event_type);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reimburse_app') THEN
        CREATE ROLE reimburse_app LOGIN PASSWORD 'CHANGE_ME_OUT_OF_BAND';
    END IF;
END $$;

REVOKE ALL ON audit_events FROM PUBLIC;
REVOKE ALL ON audit_events FROM reimburse_app;
GRANT SELECT, INSERT ON audit_events TO reimburse_app;
-- INSERT ... ON CONFLICT DO NOTHING needs only INSERT. The identity column needs no sequence grant.
