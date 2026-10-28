"""Create or update one login account. Run manually with the OWNER role, never from the app:

    uv --directory web run python -m reimburse_web.seed_users --dsn "$OWNER_DATABASE_URL" --employee EMP-100 --role reviewer

The email comes from data/employees.json; the password is read with getpass (never an argument, never
printed). Upsert by employee_id: re-running rotates the password and role and re-activates the account.
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


def employee_email(employee_id: str, path: Path = EMPLOYEES) -> str:
    for row in json.loads(path.read_text(encoding="utf-8")):
        if row["id"] == employee_id:
            return normalize_email(row["email"])
    raise SystemExit(f"{employee_id} is not in {path.name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", required=True, help="OWNER connection string (not stored anywhere)")
    ap.add_argument("--employee", required=True, help="EMP-xxx, as in data/employees.json")
    ap.add_argument("--role", required=True, choices=ROLES)
    args = ap.parse_args(argv)

    email = employee_email(args.employee)
    password = getpass.getpass(f"Password for {args.employee}: ")
    if len(password) < MIN_PASSWORD_CHARS:
        raise SystemExit(f"password must be at least {MIN_PASSWORD_CHARS} characters")
    if getpass.getpass("Repeat password: ") != password:
        raise SystemExit("passwords do not match")

    import psycopg  # optional dependency, imported only when used
    with psycopg.connect(args.dsn, autocommit=True, prepare_threshold=None) as conn:
        conn.execute(UPSERT, (args.employee, email, hash_password(password), args.role))
    print(f"{args.employee} saved as {args.role}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
