"""Agent-side compact mode: shrink MCP tool outputs before they reach the model.

The server is unchanged (version A / WorkBuddy see the same outputs). Here we:
- unwrap the adapter's [{"type": "text", "text": ...}] envelope instead of JSON-encoding it again;
- minify JSON (the server pretty-prints with indent=2);
- drop null / empty values and per-finding fields that duplicate the human message
  (`key`, `params`, `resolved_by`); `rule`, `status`, `message`, `gap` stay;
- trim long free-text fields (mailbox `snippet`/`body`) to 280 chars.
Every ID, status, gap code and money figure is kept.
"""

from __future__ import annotations

import json
from typing import Any

DROP_IN_FINDINGS = {"key", "params", "resolved_by"}
TRIM_FIELDS = {"snippet": 280, "body": 280}


def tool_text(res: Any) -> str:
    """The plain text of an MCP tool result, whatever adapter shape it arrives in."""
    if isinstance(res, tuple):
        res = res[0]
    if isinstance(res, list) and res and all(isinstance(p, dict) and p.get("type") == "text" for p in res):
        texts = [p["text"] for p in res]
        if len(texts) == 1:
            return texts[0]
        try:  # a list-returning tool arrives as one text block per element
            return json.dumps([json.loads(t) for t in texts], ensure_ascii=False)
        except ValueError:
            return "\n".join(texts)
    return res if isinstance(res, str) else json.dumps(res, default=str, ensure_ascii=False)


def _prune(x: Any, parent: str = "") -> Any:
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if parent == "findings" and k in DROP_IN_FINDINGS:
                continue
            v = _prune(v, k)
            if v is None or v == [] or v == {}:
                continue
            if k in TRIM_FIELDS and isinstance(v, str) and len(v) > TRIM_FIELDS[k]:
                v = v[: TRIM_FIELDS[k]] + "..."
            out[k] = v
        return out
    if isinstance(x, list):
        return [_prune(v, parent) for v in x]
    return x


def compact(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return text  # e.g. draft_decision returns a prose note: keep it verbatim
    return json.dumps(_prune(data), ensure_ascii=False, separators=(",", ":"))
