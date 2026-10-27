"""Read-only Q&A tools, scoped to the signed-in employee by code.

The identity is a `Session` the web layer builds from its own session cookie. No tool takes an
employee_id parameter: `QAToolbox.call` drops any argument outside a tool's declared parameters (and records
that it did), so a model cannot widen the scope by naming someone else. A claim that belongs to someone
else and a claim that does not exist return the same bytes (NOT_FOUND).

There are no write tools. READ_TOOLS is the complete list a model ever sees.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Protocol

from .policy import Rulebook

READ_TOOLS = ("my_claims", "my_claim", "my_claim_check", "search_policy")
TOOL_SPECS = [
    {"name": "my_claims", "description": "List the signed-in employee's own claims.", "parameters": {}},
    {"name": "my_claim", "description": "One of the signed-in employee's own claims, with its items.",
     "parameters": {"claim_id": {"type": "string"}}},
    {"name": "my_claim_check", "description": "What the automated check found on one of the employee's own claims "
     "(employee-safe: findings under finance review are not detailed).", "parameters": {"claim_id": {"type": "string"}}},
    {"name": "search_policy", "description": "Search the claim policy rulebook; returns numbered articles.",
     "parameters": {"query": {"type": "string"}}},
]
PARAMS = {t["name"]: frozenset(t["parameters"]) for t in TOOL_SPECS}

NOT_FOUND = {"error": "not_found",
             "message_id": "Klaim tidak ditemukan di antara klaim Anda.",
             "message_en": "Claim not found among your claims."}
CLAIM_ID_RE = re.compile(r"^CLM-[A-Za-z0-9-]{1,40}$")

# Findings an employee sees only as "under finance review" (suspected fraud signals or reviewer work).
SENSITIVE_GAPS = frozenset({"duplicate_suspected", "receipt_amount_mismatch", "multiple_receipts"})
SENSITIVE_KEYS = frozenset({"dup_suspected", "dup_same_receipt", "receipt_amount_mismatch", "multiple_receipts",
                            "receipt_shared", "receipt_amount_unreadable", "receipt_amount_missing",
                            "human_resolved", "human_rejected"})
UNDER_REVIEW = {"status": "under_finance_review", "note_id": "Sedang ditinjau finance.",
                "note_en": "Under finance review."}
STATUS_LABEL = {  # engine decision -> employee wording (never a promise)
    "approve": ("lolos pemeriksaan otomatis, menunggu keputusan finance", "passed the automated check, awaiting finance's decision"),
    "partial_approve": ("sebagian item melebihi kebijakan, menunggu keputusan finance", "some items exceed the policy, awaiting finance's decision"),
    "reject": ("ada item yang tidak sesuai kebijakan, menunggu keputusan finance", "some items do not meet the policy, awaiting finance's decision"),
    "needs_info": ("perlu kelengkapan atau sedang ditinjau", "needs more documents or is under review"),
}
GAP_NOTE = {  # gap code -> what the employee can do about it
    "boarding_pass_missing": ("Boarding pass belum ada. Kirim boarding pass yang cocok (POL-6).",
                              "Boarding pass missing. Send the matching boarding pass (POL-6)."),
    "receipt_missing": ("Bukti pembayaran belum ada. Kirim kwitansi/struk (POL-6).",
                        "Receipt missing. Send the receipt (POL-6)."),
    "hotel_over_cap": ("Melebihi plafon hotel; perlu memo persetujuan yang ditandatangani (POL-4).",
                       "Over the hotel cap; needs a signed approval memo (POL-4)."),
    "late_checkout_unjustified": ("Late check-out perlu dasar rapat dinas (POL-9).",
                                  "Late check-out needs a business-meeting justification (POL-9)."),
}


@dataclass(frozen=True)
class Session:
    """Who is asking. Built by the web layer from its session, never from message text."""
    employee_id: str
    session_id: str


class ClaimData(Protocol):
    def claims(self) -> list[dict]: ...
    def check(self, claim_id: str) -> dict: ...  # jsonable ClaimCheck (reimburse_buddy.models.to_jsonable)


Render = Callable[[str, dict, str], str]  # (finding key, params, "en"|"id") -> text


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class QAToolbox:
    def __init__(self, session: Session, data: ClaimData, rulebook: Rulebook, render: Render | None = None,
                 max_calls: int = 6):
        self.session = session
        self.max_calls = max_calls
        self._data = data
        self.rulebook = rulebook
        self._render = render
        self.calls: list[dict] = []  # {"tool", "args", "dropped_args", "output"}

    # --- dispatch ---------------------------------------------------------------------------------------
    def call(self, tool: str, args: dict | None = None) -> str:
        """Run one read tool. Unknown tools are refused; undeclared args (employee_id, ...) are dropped."""
        args = dict(args or {})
        if tool not in READ_TOOLS or len(self.calls) >= self.max_calls:
            out = _dump({"error": "unknown_tool", "allowed": list(READ_TOOLS)} if tool not in READ_TOOLS
                        else {"error": "tool_call_limit", "max": self.max_calls})
            self.calls.append({"tool": tool, "args": {}, "dropped_args": sorted(args), "output": out})
            return out
        kept = {k: v for k, v in args.items() if k in PARAMS[tool]}
        out = _dump(getattr(self, "_" + tool)(**{k: str(v) for k, v in kept.items()}))
        self.calls.append({"tool": tool, "args": kept, "dropped_args": sorted(set(args) - set(kept)), "output": out})
        return out

    def outputs(self) -> list[str]:
        return [c["output"] for c in self.calls]

    def own_claim_ids(self) -> set[str]:
        """For the output leak guard (not a tool, not logged as a call)."""
        return {c["id"] for c in self._own()}

    # --- scoping ----------------------------------------------------------------------------------------
    def _own(self) -> list[dict]:
        return [c for c in self._data.claims() if c.get("employee_id") == self.session.employee_id]

    def _own_claim(self, claim_id: str) -> dict | None:
        if not CLAIM_ID_RE.match(claim_id or ""):
            return None
        return next((c for c in self._own() if c["id"] == claim_id), None)

    # --- tools ------------------------------------------------------------------------------------------
    def _my_claims(self) -> dict:
        return {"claims": [{"claim_id": c["id"], "status": c.get("status"), "purpose": c.get("purpose"),
                            "destination": c["trip"]["destination"], "start_date": c["trip"]["start_date"],
                            "end_date": c["trip"]["end_date"]} for c in self._own()]}

    def _my_claim(self, claim_id: str = "") -> dict:
        c = self._own_claim(claim_id)
        if c is None:
            return NOT_FOUND
        return {"claim_id": c["id"], "status": c.get("status"), "purpose": c.get("purpose"),
                "destination": c["trip"]["destination"], "start_date": c["trip"]["start_date"],
                "end_date": c["trip"]["end_date"],
                "items": [{"item_id": i["id"], "category": i["category"], "date": i.get("date"),
                           "description": i.get("description"), "amount": i.get("amount")} for i in c["items"]]}

    def _my_claim_check(self, claim_id: str = "") -> dict:
        c = self._own_claim(claim_id)
        if c is None:
            return NOT_FOUND
        return employee_safe_check(self._data.check(c["id"]), self._render)

    def _search_policy(self, query: str = "") -> dict:
        hits = self.rulebook.search(query)
        return {"rulebook": self.rulebook.version, "articles": [
            {"article_id": a["id"], "pasal": a["number"], "title_id": a["title_id"], "title_en": a["title_en"],
             "text_id": a["text_id"], "text_en": a["text_en"]} for a in hits]}


def is_sensitive_item(item: dict) -> bool:
    """Single source of truth: does this (jsonable) rule-engine item carry a finding an employee may only
    see as UNDER_REVIEW? Used by my_claim_check and the employee claim page."""
    return bool(SENSITIVE_GAPS & set(item.get("gaps") or [])) or any(
        f.get("key") in SENSITIVE_KEYS or f.get("gap") in SENSITIVE_GAPS for f in item.get("findings") or [])


def employee_safe_check(check: dict, render: Render | None = None) -> dict:
    """The rule engine's result as an employee may see it: no approvable/pending totals, no reviewer
    decisions, and every item with a sensitive finding reduced to UNDER_REVIEW."""
    items, any_sensitive = [], False
    for it in check.get("items", []):
        base = {"item_id": it["item_id"], "category": it["category"], "claimed": it.get("claimed")}
        if is_sensitive_item(it):
            any_sensitive = True
            items.append({**base, **UNDER_REVIEW})
            continue
        notes_id, notes_en = [], []
        for g in it.get("gaps") or []:
            if g in GAP_NOTE:
                notes_id.append(GAP_NOTE[g][0])
                notes_en.append(GAP_NOTE[g][1])
        if it.get("status") == "rejected":
            for f in it.get("findings") or []:
                if f.get("status") == "rejected" and f.get("key") not in SENSITIVE_KEYS:
                    en = render(f["key"], f.get("params") or {}, "en") if render else f.get("message", "")
                    idt = render(f["key"], f.get("params") or {}, "id") if render else en
                    notes_id.append(f"Tidak sesuai kebijakan: {idt}")
                    notes_en.append(f"Does not meet the policy: {en}")
        engine_status = it.get("status")
        status = engine_status if engine_status in ("ok", "rejected") else "needs_action" if notes_id else None
        if status is None:  # needs evidence / partial for a reason we do not phrase for employees
            any_sensitive = True
            items.append({**base, **UNDER_REVIEW})
            continue
        items.append({**base, "status": status, "note_id": " ".join(notes_id), "note_en": " ".join(notes_en)})
    decision = check.get("decision", "needs_info")
    label = STATUS_LABEL.get(decision, STATUS_LABEL["needs_info"])
    return {"claim_id": check.get("claim_id"), "check_status": decision, "status_id": label[0], "status_en": label[1],
            "has_items_under_review": any_sensitive, "total_claimed": check.get("total_claimed"), "items": items}
