"""The worker reports the innermost cause of an ExceptionGroup, without DSNs or secrets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import investigate_worker as W  # noqa: E402


def test_innermost_cause_of_nested_groups():
    inner = ModuleNotFoundError("No module named 'psycopg'")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("inner", [inner])])
    assert W.error_message(group) == "ModuleNotFoundError: No module named 'psycopg'"


def test_plain_exception_keeps_its_type_and_message():
    assert W.error_message(RuntimeError("boom")) == "RuntimeError: boom"


def test_dsn_and_secrets_are_removed(monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "abcd1234efgh5678")
    exc = ExceptionGroup("g", [OSError(
        "cannot connect to postgresql://user:pw@host/db password=hunter2 key abcd1234efgh5678")])
    msg = W.error_message(exc)
    for leak in ("user:pw", "hunter2", "abcd1234efgh5678", "postgresql://"):
        assert leak not in msg
    assert msg.startswith("OSError: cannot connect")
