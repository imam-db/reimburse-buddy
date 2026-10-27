-- LLM spend ledger, version B. Run as the OWNER role (migrations only). The app connects as
-- reimburse_app, which can INSERT and SELECT but never UPDATE or DELETE: like the audit log, the ledger
-- is append-only, so a charge can never be edited away.
--
-- Separate from audit_events on purpose: a charge is operational bookkeeping, not an audit event, and
-- the daily cap has to be computed with one locked read, which an append-only event table with a
-- payload blob cannot do cheaply. The cap itself is enforced in web/reimburse_web/spend.py.
--
-- One row per decision:
--   kind = 'reserve'  worst case of one run, written before the worker starts
--   kind = 'settle'   real cost of that run, written when the worker reports tokens
-- Committed for a day = SUM(settle) + SUM(reserve of runs that never settled).

CREATE TABLE IF NOT EXISTS llm_spend (
    seq     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- insertion order
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    day     DATE        NOT NULL,                              -- Asia/Jakarta calendar day
    run_id  TEXT        NOT NULL,
    kind    TEXT        NOT NULL CHECK (kind IN ('reserve', 'settle')),
    usd     NUMERIC(12, 6) NOT NULL CHECK (usd >= 0),
    tokens  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CHECK (kind <> 'reserve' OR tokens = '{}'::jsonb)
);
CREATE INDEX IF NOT EXISTS llm_spend_day ON llm_spend (day, kind, run_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reimburse_app') THEN
        CREATE ROLE reimburse_app LOGIN PASSWORD 'CHANGE_ME_OUT_OF_BAND';
    END IF;
END $$;

REVOKE ALL ON llm_spend FROM PUBLIC;
REVOKE ALL ON llm_spend FROM reimburse_app;
GRANT SELECT, INSERT ON llm_spend TO reimburse_app;
-- The identity column needs no sequence grant.
