"""Render a reviewer-ready decision note from a ClaimCheck. Text only, no new numbers.

language: "en", "id" (Bahasa Indonesia) or "both" (Indonesian first, then English).
"""

from __future__ import annotations

from .data_store import DataStore
from .messages import IGNORED, RESOLVED_PREFIX, CHOICES_ID, render
from .models import ClaimCheck, Decision, ItemStatus, rupiah

LABELS = {
    "en": {
        Decision.APPROVE: "APPROVED", Decision.PARTIAL: "PARTIALLY APPROVED",
        Decision.REJECT: "REJECTED", Decision.NEEDS_INFO: "ON HOLD - NEEDS INFORMATION",
        "title": "Expense claim", "trip": "Trip", "to": "to", "decision": "Decision", "claimed": "Claimed",
        "approved": "Approved", "pending": "Pending", "items": "Items", "evidence": "Evidence",
        "note": "Reviewer note", "warnings": "Warnings", "ignored": "Evidence checked but not used",
        "none": "No issues.",
        "status": {"ok": "ok", "partial": "partial", "rejected": "rejected", "needs_evidence": "needs evidence"},
    },
    "id": {
        Decision.APPROVE: "DISETUJUI", Decision.PARTIAL: "DISETUJUI SEBAGIAN",
        Decision.REJECT: "DITOLAK", Decision.NEEDS_INFO: "DITUNDA - PERLU INFORMASI",
        "title": "Klaim biaya", "trip": "Perjalanan", "to": "s.d.", "decision": "Keputusan", "claimed": "Diajukan",
        "approved": "Disetujui", "pending": "Tertunda", "items": "Rincian", "evidence": "Bukti",
        "note": "Catatan reviewer", "warnings": "Peringatan", "ignored": "Bukti diperiksa tetapi tidak dipakai",
        "none": "Tidak ada masalah.",
        "category": {"flight": "penerbangan", "hotel": "hotel", "late_checkout": "late check-out", "taxi": "taksi",
                     "per_diem": "uang harian", "meal": "makan"},
        "status": {"ok": "sesuai", "partial": "sebagian", "rejected": "ditolak", "needs_evidence": "perlu bukti"},
    },
}


def _item_text(r, lang: str) -> str:
    idx = 0 if lang == "en" else 1
    parts = []
    for f in r.findings:
        if f.status == ItemStatus.OK and r.status != ItemStatus.OK:
            continue
        text = render(f.key, f.params, lang).rstrip(".")
        if f.resolved_by:
            choice = f.resolved_by if lang == "en" else CHOICES_ID[f.resolved_by]
            text = RESOLVED_PREFIX[idx].format(choice=choice) + (text if text.startswith("Rp") else text[0].lower() + text[1:])
        if f.key == "human_resolved" and any(x.resolved_by for x in r.findings):
            continue  # already shown as the resolved prefix
        parts.append(text)
    return "; ".join(parts) + "." if parts else ""


def _render(store: DataStore, check: ClaimCheck, note: str, lang: str) -> str:
    L = LABELS[lang]
    claim = store.get_claim(check.claim_id)
    emp = store.get_employee(check.employee_id)
    t = claim["trip"]
    lines = [
        f"{L['title']} {check.claim_id} - {emp['name']} ({emp['department']})",
        f"{L['trip']}: {t['origin']} -> {t['destination']}, {t['start_date']} {L['to']} {t['end_date']}",
        f"{L['decision']}: {L[check.decision]}",
        f"{L['claimed']}: {rupiah(check.total_claimed)} | {L['approved']}: {rupiah(check.total_approvable)}"
        + (f" | {L['pending']}: {rupiah(check.total_pending)}" if check.total_pending else ""),
        "",
        f"{L['items']}:",
    ]
    for r in check.items:
        msg = _item_text(r, lang) or L["none"]
        lines.append(f"- {r.item_id} {L.get('category', {}).get(r.category, r.category)}: {rupiah(r.claimed)} -> {rupiah(r.approvable)} "
                     f"[{L['status'][r.status.value]}] {msg}")
    if check.warnings:
        lines += ["", f"{L['warnings']}: " + " ".join(render(w.key, w.params, lang) for w in check.warnings)]
    used = sorted({e for r in check.items for e in r.evidence_used})
    if used:
        lines += ["", f"{L['evidence']}: " + ", ".join(used)]
    if check.ignored_evidence:
        idx = 0 if lang == "en" else 1
        lines += ["", f"{L['ignored']}:"]
        lines += [f"- {i.id}: {IGNORED[i.reason][idx]}" for i in check.ignored_evidence]
    if note:
        lines += ["", f"{L['note']}: {note}"]
    return "\n".join(lines)


def draft_decision(store: DataStore, check: ClaimCheck, note: str = "", language: str = "en") -> str:
    if language == "both":
        return _render(store, check, note, "id") + "\n\n---\n\n" + _render(store, check, note, "en")
    if language not in LABELS:
        raise ValueError("language must be 'en', 'id' or 'both'")
    return _render(store, check, note, language)
