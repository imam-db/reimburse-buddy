"""The agent worker command: --extra postgres only when the audit DSN is set."""
from reimburse_web.agent_bridge import Investigator


def test_command_without_dsn_is_unchanged(monkeypatch):
    monkeypatch.delenv("REIMBURSE_AUDIT_DATABASE_URL", raising=False)
    cmd = Investigator().command("CLM-1")
    assert "--extra" not in cmd
    i = cmd.index("--no-dev")
    assert cmd[i + 1:i + 3] == ["python", "investigate_worker.py"]


def test_command_with_dsn_adds_postgres_extra(monkeypatch):
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://u:p@h/db")
    cmd = Investigator().command("CLM-1", "/tmp/snap")
    i = cmd.index("--extra")
    assert cmd[i:i + 2] == ["--extra", "postgres"]
    assert i < cmd.index("python") and "--no-dev" in cmd[:i]
    assert "postgresql://u:p@h/db" not in " ".join(cmd)


def test_blank_dsn_counts_as_unset(monkeypatch):
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "  ")
    assert "--extra" not in Investigator().command("CLM-1")
