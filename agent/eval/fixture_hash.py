"""One hash of every input that defines the eval cases, stored in each results file, so a result can be tied to
the exact fixtures it ran on (and fixtures can't be quietly re-tuned after a run)."""

from __future__ import annotations

import hashlib
from pathlib import Path

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parents[1]
SOURCES = [EVAL / "fixtures", ROOT / "data", EVAL / "hard_fixtures.py", EVAL / "large_fixtures.py", EVAL / "hard_cases.py", EVAL / "cases.py"]


def fixture_files() -> list[Path]:
    out = []
    for p in SOURCES:
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix in (".json", ".py", ".jsonl"))
        elif p.is_file():
            out.append(p)
    return out


def fixture_hash() -> dict:
    """{"sha256": combined, "files": {relative path: sha256}}."""
    files = {}
    h = hashlib.sha256()
    for f in fixture_files():
        rel = f.relative_to(ROOT).as_posix()
        d = hashlib.sha256(f.read_bytes()).hexdigest()
        files[rel] = d
        h.update(f"{rel}\0{d}\n".encode())
    return {"sha256": h.hexdigest(), "files": files}


if __name__ == "__main__":
    print(fixture_hash()["sha256"])
