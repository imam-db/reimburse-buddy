"""Render a reviewer-ready decision note from a ClaimCheck. Text only, no new numbers."""

from __future__ import annotations

from .data_store import DataStore
from .models import ClaimCheck, Decision, ItemStatus, rupiah


def draft_decision(store: DataStore, check: ClaimCheck, note: str = "") -> str:
    claim = store.get_claim(check.claim_id)
    emp = store.get_employee(check.employee_id)
    label = {
        Decision.APPROVE: "APPROVED",
        Decision.PARTIAL: "PARTIALLY APPROVED",
        Decision.REJECT: "REJECTED",
        Decision.NEEDS_INFO: "ON HOLD - NEEDS INFORMATION",
    }[check.decision]
    lines = [
        f"Expense claim {check.claim_id} - {emp['name']} ({emp['department']})",
        f"Trip: {claim['trip']['origin']} -> {claim['trip']['destination']}, "
        f"{claim['trip']['start_date']} to {claim['trip']['end_date']}",
        f"Decision: {label}",
        f"Claimed: {rupiah(check.total_claimed)} | Approved: {rupiah(check.total_approvable)}"
        + (f" | Pending: {rupiah(check.total_pending)}" if check.total_pending else ""),
        "",
        "Items:",
    ]
    for r in check.items:
        msg = "; ".join(f.message for f in r.findings if f.status != ItemStatus.OK or r.status == ItemStatus.OK) or "OK"
        lines.append(f"- {r.item_id} {r.category}: {rupiah(r.claimed)} -> {rupiah(r.approvable)} [{r.status.value}] {msg}")
    used = sorted({e for r in check.items for e in r.evidence_used})
    if used:
        lines += ["", "Evidence: " + ", ".join(used)]
    if note:
        lines += ["", f"Reviewer note: {note}"]
    return "\n".join(lines)
