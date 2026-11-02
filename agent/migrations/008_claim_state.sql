-- Claim state for the web app (version B), ADR 0010. Run as the OWNER role (migrations only). The app
-- connects as reimburse_app, which can SELECT, INSERT and UPDATE these tables but never DELETE or TRUNCATE:
-- nothing a person did to a claim is removed by the application.
--
-- Why these tables exist: everything a person does to a claim in the web app lived in the web process and
-- was gone after a restart or a redeploy. The audit log in Neon already held the manager answers and the
-- plan events that point at those claims, so the two disagreed. Now every web write goes here first and to
-- memory second, and startup replays the rows on top of the JSON seed.
--
-- The seed (data/*.json: employees, policy, the demo claims, the seed mailbox, calendar, memos, scripted
-- replies) stays in git and is never written here.
--
-- One row per fact, never a blob of everything. The JSON column holds the same dict shape the web code
-- already uses, so there is no mapping layer. It is JSON, not JSONB, on purpose: JSONB reorders object keys,
-- and the pages render dicts (provenance tags, pending evidence, decisions) in the order they were written,
-- so a replay from JSONB would change what a page shows after a restart. Nothing queries inside the payload.
--   demo_epoch           the epoch the other tables are read at (Reset demo moves to a new one)
--   web_claims           a claim an employee submitted, same shape as a claims.json entry
--   web_messages         a mailbox message the web app added (uploads, delivered scripted replies)
--   claim_reviews        the REVIEW[cid] dict: evidence, decisions, pending, evidence_log, origin
--   employee_drafts      a drop-first draft; submitted_at is set on submit instead of deleting the row
--   outbox_messages      a message finance sent to an employee (the simulated outbox)
--   approval_requests    a request finance asked a manager, updated when the manager answers
--   claim_status_changes one status change, replayed in order (identity key)
--   uploaded_files       the bytes of an upload, so a host with an ephemeral disk keeps the receipts
--
-- Reset demo cannot delete rows: it increments demo_epoch, and every table reads as empty for the new
-- epoch while the old rows stay in place. Cleanup of old epochs is an owner-role task, never the app.
--
-- audit_events, llm_spend, the plan-state tables and the checkpoint tables are not touched.

CREATE TABLE IF NOT EXISTS demo_epoch (
    id    INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    epoch BIGINT  NOT NULL DEFAULT 1 CHECK (epoch >= 1)
);
INSERT INTO demo_epoch (id, epoch) VALUES (1, 1) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS web_claims (
    epoch       BIGINT      NOT NULL,
    claim_id    TEXT        NOT NULL,
    employee_id TEXT        NOT NULL,
    claim       JSON        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (epoch, claim_id)
);
CREATE INDEX IF NOT EXISTS web_claims_employee ON web_claims (epoch, employee_id);

CREATE TABLE IF NOT EXISTS web_messages (
    epoch      BIGINT      NOT NULL,
    message_id TEXT        NOT NULL,
    owner_id   TEXT        NOT NULL,
    claim_id   TEXT        NOT NULL DEFAULT '',
    message    JSON        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (epoch, message_id)
);
CREATE INDEX IF NOT EXISTS web_messages_owner ON web_messages (epoch, owner_id);
CREATE INDEX IF NOT EXISTS web_messages_claim ON web_messages (epoch, claim_id);

CREATE TABLE IF NOT EXISTS claim_reviews (
    epoch      BIGINT      NOT NULL,
    claim_id   TEXT        NOT NULL,
    review     JSON        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (epoch, claim_id)
);

CREATE TABLE IF NOT EXISTS employee_drafts (
    epoch        BIGINT      NOT NULL,
    draft_id     TEXT        NOT NULL,
    employee_id  TEXT        NOT NULL,
    draft        JSON        NOT NULL,
    submitted_at TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (epoch, draft_id)
);
CREATE INDEX IF NOT EXISTS employee_drafts_employee ON employee_drafts (epoch, employee_id);

CREATE TABLE IF NOT EXISTS outbox_messages (
    epoch      BIGINT      NOT NULL,
    message_id TEXT        NOT NULL,
    message    JSON        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (epoch, message_id)
);

CREATE TABLE IF NOT EXISTS approval_requests (
    epoch      BIGINT      NOT NULL,
    request_id TEXT        NOT NULL,
    claim_id   TEXT        NOT NULL DEFAULT '',
    request    JSON        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (epoch, request_id)
);
CREATE INDEX IF NOT EXISTS approval_requests_claim ON approval_requests (epoch, claim_id);

CREATE TABLE IF NOT EXISTS claim_status_changes (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    epoch       BIGINT      NOT NULL,
    claim_id    TEXT        NOT NULL,
    from_status TEXT        NOT NULL,
    to_status   TEXT        NOT NULL,
    note        TEXT        NOT NULL DEFAULT '',
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claim_status_changes_epoch ON claim_status_changes (epoch, id);

CREATE TABLE IF NOT EXISTS uploaded_files (
    epoch        BIGINT      NOT NULL,
    stored_as    TEXT        NOT NULL,
    owner_id     TEXT        NOT NULL,
    content_type TEXT        NOT NULL DEFAULT 'application/octet-stream',
    size_bytes   BIGINT      NOT NULL,
    sha256       TEXT        NOT NULL DEFAULT '',
    content      BYTEA       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (epoch, stored_as)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reimburse_app') THEN
        CREATE ROLE reimburse_app LOGIN PASSWORD 'CHANGE_ME_OUT_OF_BAND';
    END IF;
END $$;

REVOKE ALL ON demo_epoch, web_claims, web_messages, claim_reviews, employee_drafts, outbox_messages,
    approval_requests, claim_status_changes, uploaded_files FROM PUBLIC;
REVOKE ALL ON demo_epoch, web_claims, web_messages, claim_reviews, employee_drafts, outbox_messages,
    approval_requests, claim_status_changes, uploaded_files FROM reimburse_app;
GRANT SELECT, INSERT, UPDATE ON demo_epoch, web_claims, web_messages, claim_reviews, employee_drafts,
    outbox_messages, approval_requests, claim_status_changes, uploaded_files TO reimburse_app;
-- No DELETE and no TRUNCATE: a submitted claim, a reviewer's decision and a forwarded receipt are part of
-- the record of what happened. Reset demo moves demo_epoch forward instead of removing rows.

-- claim_status_changes gets its key from an identity column, so the app role needs the sequence.
DO $$
DECLARE seq TEXT;
BEGIN
    SELECT pg_get_serial_sequence('claim_status_changes', 'id') INTO seq;
    IF seq IS NOT NULL THEN
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO reimburse_app', seq);
    END IF;
END $$;
