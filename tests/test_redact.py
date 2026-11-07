"""agent_bridge.redact: secrets never reach the finance investigation page."""

from reimburse_web.agent_bridge import redact


def test_redact_scrubs_session_key_dsn_values_and_url_passwords(monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "sessionkeyvalue1234")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://u:pw@host/db?sslmode=require")
    out = redact("key=sessionkeyvalue1234 dsn=postgresql://u:pw@host/db?sslmode=require "
                 "other postgresql://bob:hunter2@h2:5432/x ok")
    assert "sessionkeyvalue1234" not in out and "hunter2" not in out and ":pw@" not in out
    assert redact("postgresql://user:SECRET@host/db") == "postgresql://user:***@host/db"

