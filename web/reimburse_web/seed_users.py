"""Create or update login accounts. Run manually with the OWNER role, never from the app:

    uv --directory web run python -m reimburse_web.seed_users --dsn "$OWNER_DATABASE_URL" --employee EMP-100 --role reviewer
    uv --directory web run python -m reimburse_web.seed_users --dsn "$OWNER_DATABASE_URL" --all
    uv --directory web run python -m reimburse_web.seed_users --all --dry-run

--all makes one account per row of data/employees.json, with the role from derive_role(). --dry-run only prints
employee_id, email and role: no password prompt, no database. --same-password (with --all) asks once and uses that
password for every account; each account still gets its own salt.

The email comes from data/employees.json; passwords are read with getpass (never an argument, never printed).
Upsert by employee_id: re-running rotates the password and role and re-activates the account.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from .users import ROLES, hash_password, normalize_email

EMPLOYEES = Path(__file__).resolve().parents[2] / "data" / "employees.json"
MIN_PASSWORD_CHARS = 12
UPSERT = ("INSERT INTO app_users (employee_id, email, password_hash, role) VALUES (%s, %s, %s, %s)"
          " ON CONFLICT (employee_id) DO UPDATE SET email = EXCLUDED.email,"
          " password_hash = EXCLUDED.password_hash, role = EXCLUDED.role, is_active = TRUE")


def load_rows(path: Path = EMPLOYEES) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def employee_email(employee_id: str, path: Path = EMPLOYEES) -> str:
    for row in load_rows(path):
        if row["id"] == employee_id:
            return normalize_email(row["email"])
    raise SystemExit(f"{employee_id} is not in {path.name}")


def derive_role(row: dict) -> str:
    """Finance managers approve as finance, other managers as reviewer, everyone else is an employee."""
    if row.get("grade") == "manager":
        return "finance" if row.get("department") == "Finance" else "reviewer"
    return "employee"


def read_password(label: str) -> str:
    """Ask twice with getpass; SystemExit if the password is too short or the two entries differ."""
    password = getpass.getpass(f"Password for {label}: ")
    if len(password) < MIN_PASSWORD_CHARS:
        raise SystemExit(f"password must be at least {MIN_PASSWORD_CHARS} characters")
    if getpass.getpass(f"Repeat password for {label}: ") != password:
        raise SystemExit("passwords do not match")
    return password


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", help="OWNER connection string (not stored anywhere); required unless --dry-run")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="one account per row of data/employees.json")
    mode.add_argument("--employee", help="EMP-xxx, as in data/employees.json")
    ap.add_argument("--role", choices=ROLES, help="role for --employee (derived per row with --all)")
    ap.add_argument("--same-password", action="store_true",
                    help="with --all: one password for every account (demo convenience)")
    ap.add_argument("--dry-run", action="store_true", help="print the accounts; no password, no database")
    args = ap.parse_args(argv)

    if not args.all and not args.employee:
        ap.error("give either --all or --employee EMP-xxx --role <role>")
    if args.all and args.role:
        ap.error("--role cannot be combined with --all (the role is derived per row)")
    if args.employee and not args.role:
        ap.error("--employee needs --role")
    if args.same_password and not args.all:
        ap.error("--same-password only applies with --all")
    if not args.dry_run and not args.dsn:
        ap.error("--dsn is required unless --dry-run")

    if args.all:
        accounts = [(row["id"], normalize_email(row["email"]), derive_role(row)) for row in load_rows()]
    else:
        accounts = [(args.employee, employee_email(args.employee), args.role)]

    if args.dry_run:
        for employee_id, email, role in accounts:
            print(f"{employee_id}  {email}  {role}")
        return 0

    # Collect and hash every password before connecting, so a typo on account 5 writes nothing.
    if args.same_password:
        shared = read_password("all accounts")
        passwords = [shared] * len(accounts)
    else:
        passwords = [read_password(employee_id) for employee_id, _, _ in accounts]
    saved = [(employee_id, email, hash_password(pw), role)
             for (employee_id, email, role), pw in zip(accounts, passwords)]

    import psycopg  # optional dependency, imported only when used
    with psycopg.connect(args.dsn, autocommit=True, prepare_threshold=None) as conn:
        for employee_id, email, digest, role in saved:
            conn.execute(UPSERT, (employee_id, email, digest, role))
            print(f"{employee_id} saved as {role}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
