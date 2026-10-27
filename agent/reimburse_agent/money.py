"""The "invented Rp" check, shared by the eval scorer and the runtime Employee Q&A guard.

An answer may quote a Rupiah figure only if that figure appears in tool output: as an Rp-formatted
amount in text, or as the value of a money-named field in tool JSON. IDs, dates and counts never
whitelist a figure.
"""

from __future__ import annotations

import json
import re

RP = re.compile(r"Rp\s?(\d[\d.,]*\d|\d)")
# Keys whose values are money in tool JSON (check_claim, draft_decision, get_claim, get_policy, ...).
MONEY_KEY = re.compile(r"amount|total|claimed|approvable|cap|limit|per_night|per_day|fee|allowance|pending|paid", re.I)


def _int(s: str) -> int | None:
    s = re.sub(r"[.,]\d{2}$", "", s)  # Rp1.200.000,00
    d = re.sub(r"[^\d]", "", s)
    return int(d) if d else None


def money_seen(tool_texts: list[str]) -> set[int]:
    """Amounts the agent may quote, from tool outputs (JSON strings or plain text)."""
    out: set[int] = set()

    def walk(v, key=""):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str):
            out.update(_int(x) for x in RP.findall(v))
            if MONEY_KEY.search(key) and re.fullmatch(r"\d[\d.,]*", v.strip()):
                out.add(_int(v))
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and MONEY_KEY.search(key):
            out.add(int(v))

    for t in tool_texts:
        try:
            walk(json.loads(t))
        except (TypeError, ValueError):
            walk(t)
    return out - {None}


def invented_rp(answer: str, tool_texts: list[str]) -> list[int]:
    """Rp figures in `answer` that no tool output supports (0 is always allowed)."""
    return sorted({_int(x) for x in RP.findall(answer or "")} - {None, 0} - money_seen(tool_texts))
