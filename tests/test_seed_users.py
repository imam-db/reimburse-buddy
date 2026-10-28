"""The account seed script: --all, --dry-run, --same-password and the role mapping (web/reimburse_web/seed_users.py)."""

import json
import sys
import types

import pytest

from reimburse_web import seed_users, users

EXPECTED = {
    "EMP-001": "employee", "EMP-002": "employee", "EMP-003": "employee", "EMP-004": "employee",
    "EMP-005": "employee", "EMP-100": "reviewer", "EMP-101": "reviewer", "EMP-102": "finance",
}
SHARED = "shared-demo-passphrase"


@pytest.fixture
def fake_psycopg(monkeypatch):
    log = {"connects": [], "executed": [], "closed": 0}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            log["closed"] += 1
            return False

        def execute(self, sql, params=()):
            log["executed"].append((sql, params))

    def connect(dsn, **kwargs):
        log["connects"].append(dsn)
        return Conn()

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=connect))
    return log


def feed_passwords(monkeypatch, answers):
    """getpass answers in order; running out of answers means the script asked for more than expected."""
    answers = iter(answers)
    monkeypatch.setattr(seed_users.getpass, "getpass", lambda prompt="": next(answers))


def per_account_answers(ids, shared=None):
    out = []
    for emp in ids:
        pw = shared or f"{emp.lower()}-own-passphrase"
        out += [pw, pw]
    return out


def test_derive_role_maps_all_eight_rows_of_employees_json():
    rows = json.loads(seed_users.EMPLOYEES.read_text(encoding="utf-8"))
    assert len(rows) == 8
    assert {row["id"]: seed_users.derive_role(row) for row in rows} == EXPECTED


def test_derive_role_only_finance_managers_get_finance():
    assert seed_users.derive_role({"department": "Finance", "grade": "staff"}) == "employee"
    assert seed_users.derive_role({"department": "Sales", "grade": "manager"}) == "reviewer"


def test_dry_run_all_prints_eight_lines_and_never_prompts_or_connects(monkeypatch, capsys):
    def no_prompt(*args, **kwargs):
        raise AssertionError("getpass must not run in a dry run")

    monkeypatch.setattr(seed_users.getpass, "getpass", no_prompt)
    monkeypatch.setitem(sys.modules, "psycopg", None)  # importing psycopg would raise ImportError
    assert seed_users.main(["--all", "--dry-run"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 8
    assert {line.split()[0]: line.split()[-1] for line in lines} == EXPECTED
    assert all("@" in line for line in lines)


def test_all_writes_eight_upserts_over_one_connection_with_the_right_roles(monkeypatch, fake_psycopg, capsys):
    ids = sorted(EXPECTED)
    feed_passwords(monkeypatch, per_account_answers(ids))
    assert seed_users.main(["--dsn", "postgresql://owner@db.example/x", "--all"]) == 0

    assert fake_psycopg["connects"] == ["postgresql://owner@db.example/x"]
    assert fake_psycopg["closed"] == 1
    executed = fake_psycopg["executed"]
    assert len(executed) == 8 and all(sql == seed_users.UPSERT for sql, _ in executed)
    assert {params[0]: params[3] for _, params in executed} == EXPECTED
    out = capsys.readouterr().out.splitlines()
    assert out == [f"{emp} saved as {EXPECTED[emp]}." for emp in ids]


def test_same_password_still_gives_every_account_its_own_hash(monkeypatch, fake_psycopg):
    feed_passwords(monkeypatch, [SHARED, SHARED])
    assert seed_users.main(["--dsn", "x", "--all", "--same-password"]) == 0
    hashes = [params[2] for _, params in fake_psycopg["executed"]]
    assert len(hashes) == 8 and len(set(hashes)) == 8
    assert all(h != SHARED and users.verify_password(SHARED, h) for h in hashes)


def test_per_account_passwords_are_each_hashed_for_their_own_account(monkeypatch, fake_psycopg):
    ids = sorted(EXPECTED)
    feed_passwords(monkeypatch, per_account_answers(ids))
    assert seed_users.main(["--dsn", "x", "--all"]) == 0
    for (_, params), emp in zip(fake_psycopg["executed"], ids):
        assert users.verify_password(f"{emp.lower()}-own-passphrase", params[2])


def test_single_account_mode_is_unchanged(monkeypatch, fake_psycopg, capsys):
    feed_passwords(monkeypatch, ["single-account-pass", "single-account-pass"])
    assert seed_users.main(["--dsn", "x", "--employee", "EMP-100", "--role", "reviewer"]) == 0
    (sql, params), = fake_psycopg["executed"]
    assert sql == seed_users.UPSERT
    assert params[0:2] == ("EMP-100", "dewi.anggraini@nusantara-niaga.example") and params[3] == "reviewer"
    assert capsys.readouterr().out == "EMP-100 saved as reviewer.\n"


@pytest.mark.parametrize("answers", [
    ["long-enough-passphrase", "long-enough-passphrase-typo"],  # mismatch
    ["too-short"],  # too short (the repeat is never asked)
])
def test_a_bad_password_exits_and_writes_nothing(monkeypatch, fake_psycopg, answers):
    feed_passwords(monkeypatch, answers)
    with pytest.raises(SystemExit):
        seed_users.main(["--dsn", "x", "--employee", "EMP-100", "--role", "reviewer"])
    assert fake_psycopg["connects"] == [] and fake_psycopg["executed"] == []


def test_a_bad_password_on_a_later_account_writes_nothing_for_any_account(monkeypatch, fake_psycopg):
    ids = sorted(EXPECTED)
    answers = per_account_answers(ids[:-1]) + ["long-enough-passphrase", "not-the-same-one-here"]
    feed_passwords(monkeypatch, answers)
    with pytest.raises(SystemExit):
        seed_users.main(["--dsn", "x", "--all"])
    assert fake_psycopg["connects"] == [] and fake_psycopg["executed"] == []


def test_all_with_employee_is_an_argparse_error(monkeypatch, capsys):
    def no_prompt(*args, **kwargs):
        raise AssertionError("no prompt")

    monkeypatch.setattr(seed_users.getpass, "getpass", no_prompt)
    with pytest.raises(SystemExit) as exc:
        seed_users.main(["--dsn", "x", "--all", "--employee", "EMP-100"])
    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_same_password_without_all_is_an_error(capsys):
    with pytest.raises(SystemExit) as exc:
        seed_users.main(["--dsn", "x", "--employee", "EMP-100", "--role", "reviewer", "--same-password"])
    assert exc.value.code == 2
    assert "--same-password only applies with --all" in capsys.readouterr().err


def test_no_mode_is_an_error():
    with pytest.raises(SystemExit) as exc:
        seed_users.main(["--dsn", "x"])
    assert exc.value.code == 2


def test_dsn_is_required_unless_dry_run():
    with pytest.raises(SystemExit) as exc:
        seed_users.main(["--all"])
    assert exc.value.code == 2


def test_the_plain_password_never_reaches_stdout_or_stderr(monkeypatch, fake_psycopg, capsys):
    secret = "a-very-distinct-secret-phrase"
    feed_passwords(monkeypatch, [secret, secret])
    assert seed_users.main(["--dsn", "x", "--all", "--same-password"]) == 0
    captured = capsys.readouterr()
    assert secret not in captured.out and secret not in captured.err
    assert secret not in repr(fake_psycopg["executed"])
