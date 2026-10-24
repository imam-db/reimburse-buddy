"""Batch triage: run the rules on every claim in a batch and split clean from exceptions."""

from __future__ import annotations

from decimal import Decimal

from .data_store import DataStore
from .models import Decision
from .rules import check_claim


def triage_claims(store: DataStore, batch: str = "") -> dict:
    claims = [c for c in store.claims if c["status"] == "submitted" and (not batch or c.get("batch") == batch)]
    cleared, exceptions = [], []
    for c in claims:
        chk = check_claim(store, c["id"])
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
