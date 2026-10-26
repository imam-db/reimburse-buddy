"""Batch triage: run the rules on every claim in a batch and split clean from exceptions."""

from __future__ import annotations

from decimal import Decimal

from . import injection as inj
from .data_store import DataStore
from .models import Decision
from .rules import check_claim


DIRECTORY_DOMAINS = frozenset({"nusantara-niaga.example"})


def claim_content(store: DataStore, claim: dict) -> list[tuple[str, str]]:
    """(source_id, text) for every content field tied to a claim: its purpose, item descriptions and notes,
    and the owner's mailbox messages that name the claim. All of it is data, never an instruction."""
    cid = claim["id"]
    out = [(cid, str(claim.get("purpose") or "")), (cid, str(claim.get("note") or ""))]
    for it in claim.get("items", []):
        for k in ("description", "note", "notes"):
            if it.get(k):
                out.append((f"{cid}:{it['id']}", str(it[k])))
    for m in store.inbox:
        if m.get("owner_id") != claim["employee_id"]:
            continue
        names_claim = cid in f"{m.get('subject', '')} {m.get('body', '')}" or any(
            a.get("claim_id") == cid for a in m.get("attachments") or [])
        if names_claim:
            out.append((m["id"], f"{m.get('subject', '')}\n{m.get('body', '')}"))
    return [(sid, t) for sid, t in out if t.strip()]


def injection_flags(store: DataStore, claim: dict) -> list[dict]:
    """HIGH prompt-injection notices for a claim's content (empty when clean; LOW hits never count)."""
    notices = []
    for sid, text in claim_content(store, claim):
        n = inj.notice(inj.scan(text, sid, DIRECTORY_DOMAINS))
        if n:
            notices.append(n)
    return notices


def triage_claims(store: DataStore, batch: str = "") -> dict:
    claims = [c for c in store.claims if c["status"] == "submitted" and (not batch or c.get("batch") == batch)]
    cleared, exceptions = [], []
    for c in claims:
        chk = check_claim(store, c["id"])
        flags = injection_flags(store, c)
        if flags:  # needs_review: never in the one-click bulk approval, whatever the rules say
            row = {"claim_id": c["id"], "employee_id": c["employee_id"], "needs_review": True,
                   "gaps": sorted({g for i in chk.items for g in i.gaps} | {w.gap for w in chk.warnings if w.gap}
                                  | {"embedded_instructions"}),
                   "rejected_items": [i.item_id for i in chk.items if i.status.value == "rejected"],
                   "notices": [n["text"] for n in flags]}
            exceptions.append(row)
            continue
        if chk.decision == Decision.APPROVE and not chk.warnings:
            cleared.append(c["id"])
        else:
            row = {"claim_id": c["id"], "employee_id": c["employee_id"]}
            row["gaps"] = sorted({g for i in chk.items for g in i.gaps} | {w.gap for w in chk.warnings if w.gap})
            row["rejected_items"] = [i.item_id for i in chk.items if i.status.value == "rejected"]
            exceptions.append(row)
    minutes = Decimal(store.policy["triage"]["manual_review_minutes_per_claim"])
    return {
        "batch": batch or "all",
        "claims": len(claims),
        "auto_cleared": cleared,  # claim IDs; call check_claim on one if the reviewer asks
        "exceptions": exceptions,
        "time_saved_estimate": {
            "minutes": str(minutes * len(cleared)),
            "assumption": f"{minutes} min of manual review per auto-cleared claim. "
                          + store.policy["triage"]["note"],
        },
    }


def approve_cleared_batch(store: DataStore, batch_id: str, claim_ids, reviewer_confirmed: bool) -> dict:
    """Bulk-approve cleared claims, guarded by an explicit reviewer confirmation."""
    if not isinstance(claim_ids, list) or not all(isinstance(c, str) for c in claim_ids):
        return {"ok": False, "error": 'claim_ids must be a list of strings, e.g. ["CLM-0001", "CLM-0002"].'}
    if reviewer_confirmed is not True:
        return {"ok": False, "error": "reviewer_confirmed is not true. Show the cleared list to the reviewer, "
                                      "ask for confirmation, and call again only after they agree. Nothing changed."}
    cleared = triage_claims(store, batch_id)["auto_cleared"]
    if not cleared:
        return {"ok": False, "error": f"No claims in batch {batch_id!r} are currently cleared. Nothing changed."}
    missing, extra = sorted(set(cleared) - set(claim_ids)), sorted(set(claim_ids) - set(cleared))
    if missing or extra or len(claim_ids) != len(set(claim_ids)):
        return {"ok": False, "error": "claim_ids must exactly match the claims triage_claims currently clears. "
                                      "Re-run triage_claims and confirm the new list with the reviewer. Nothing changed.",
                "not_in_your_list": missing, "not_cleared": extra}
    for cid in cleared:
        store.update_claim_status(cid, "approved", f"Bulk-approved after reviewer confirmation (batch {batch_id})")
    return {"ok": True, "approved": cleared, "count": len(cleared)}
