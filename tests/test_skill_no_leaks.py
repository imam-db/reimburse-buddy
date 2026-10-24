"""SKILL.md must teach strategies, not the answers hidden in data/."""

import json
import re
from pathlib import Path

from reimburse_buddy.data_store import DEFAULT_DATA_DIR

SKILL = (Path(__file__).parents[1] / "skills/claim-auditor/SKILL.md").read_text()


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _secrets():
    out = set()
    for f in DEFAULT_DATA_DIR.glob("*.json"):
        for k, v in _walk(json.loads(f.read_text())):
            if not isinstance(v, str):
                continue
            if k in ("id", "flight_no", "flight", "receipt_no", "event_id", "subject", "name", "title", "passenger"):
                if k == "id" and re.fullmatch(r"IT-\d+", v):
                    continue  # generic item labels
                out.add(v)
    return out


def _compact(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def test_skill_has_no_data_ids_names_or_subjects():
    skill = _compact(SKILL)
    leaks = sorted(v for v in _secrets() if len(_compact(v)) >= 5 and _compact(v) in skill)
    assert leaks == []


def test_skill_has_no_rupiah_amounts_or_raw_numbers():
    assert not re.search(r"Rp\s?\d", SKILL)
    assert not re.search(r"\d{1,3}(\.\d{3}){1,}", SKILL)
    assert not re.search(r"\b\d{5,}\b", SKILL)


def test_skill_scopes_messaging_rule():
    assert "Allowed without asking" in SKILL
    assert "reset_demo" in SKILL
