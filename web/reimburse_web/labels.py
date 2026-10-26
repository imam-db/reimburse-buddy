"""Indonesian display labels for the technical codes the web pages render.

One dictionary, one job: turn a code from the rule engine / triage / upload flow into text a
finance reviewer or employee can read. Unknown codes fall back to a humanised form instead of
raising, so a new gap code never breaks a page.
"""

from __future__ import annotations

LABELS = {
    # gaps (src/reimburse_buddy/gaps.py)
    "boarding_pass_missing": "Boarding pass belum ada",
    "receipt_missing": "Struk belum ada",
    "hotel_over_cap": "Hotel di atas plafon",
    "hotel_nights_mismatch": "Jumlah malam tidak sesuai tugas",
    "late_checkout_unjustified": "Check-out terlambat tanpa alasan",
    "duplicate_suspected": "Perlu ditinjau finance",
    "return_flight_missing": "Penerbangan pulang belum ada",
    "receipt_amount_mismatch": "Nominal struk tidak sesuai",
    "multiple_receipts": "Beberapa struk untuk satu item",
    "receipt_shared": "Struk dipakai untuk item lain",
    "embedded_instructions": "Ada instruksi mencurigakan",
    # engine decisions and reviewer choices
    "approve": "Siap disetujui",
    "partial_approve": "Disetujui sebagian",
    "reject": "Tolak",
    "needs_info": "Butuh kelengkapan",
    "approve_full": "Setujui penuh",
    "approve_capped": "Setujui sebatas plafon",
    # item statuses (models.ItemStatus)
    "ok": "Sesuai",
    "needs_evidence": "Perlu bukti",
    "partial": "Sebagian",
    "rejected": "Ditolak",
    # claim statuses
    "submitted": "Diajukan",
    "approved": "Disetujui",
    "partially_approved": "Disetujui sebagian",
    "on_hold": "Ditahan",
    "needs_action": "Perlu tindakan",
    "under_finance_review": "Sedang ditinjau finance",
    # finding rule names (Finding.rule)
    "category_allowed": "Kategori diizinkan",
    "dates_within_trip": "Tanggal dalam perjalanan",
    "receipt_required": "Struk wajib",
    "boarding_pass_required": "Boarding pass wajib",
    "duplicate": "Duplikat",
    "hotel_nights_within_trip": "Malam dalam perjalanan",
    "hotel_nightly_cap": "Plafon hotel per malam",
    "per_diem": "Uang harian",
    "late_checkout": "Late check-out",
    "human_decision": "Keputusan reviewer",
    "return_flight": "Penerbangan pulang",
    # provenance sources (upload_flow)
    "ocr": "OCR",
    "pdf_text": "Teks PDF",
    "vision": "Vision",
    "self_declared": "Dideklarasikan sendiri",
    "none": "Tidak ada",
    # categories
    "flight": "Penerbangan",
    "hotel": "Hotel",
    "taxi": "Taksi",
    "meal": "Makan",
    # document types (extract.DOC_TYPES)
    "hotel_receipt": "Struk hotel",
    "taxi_receipt": "Struk taksi",
    "meal_receipt": "Struk makan",
    "flight_receipt": "Struk penerbangan",
    "boarding_pass": "Boarding pass",
    "other": "Lainnya",
    # evidence cross-validation reasons (upload_flow.evidence_review_reasons)
    "unreadable_file": "Berkas tidak terbaca",
    "self_declared_only": "Hanya dideklarasikan sendiri",
    "doc_type_mismatch": "Jenis dokumen tidak cocok",
    "mismatch_with_file": "Tidak cocok dengan berkas",
    "file_date_outside_trip": "Tanggal berkas di luar perjalanan",
    "implausible_amount": "Nominal tidak wajar",
    "boarding_pass_mismatch": "Boarding pass tidak cocok",
    "duplicate_file": "Berkas duplikat",
    "ocr_engine_disagreement": "Hasil OCR berbeda",
    # ignored evidence reasons (rules.py)
    "unknown_id": "ID tidak ditemukan",
    "belongs_to_other_employee": "Milik karyawan lain",
    "memo_unsigned": "Memo belum ditandatangani",
    "no_matching_item": "Tidak cocok dengan item",
    # evidence statuses and reviewer verdicts
    "pending": "Menunggu",
    "needs_review": "Perlu ditinjau",
    "extracted": "Terekstrak",
    "accepted": "Diterima",
    "accepted_by_reviewer": "Diterima reviewer",
    "rejected_by_reviewer": "Ditolak reviewer",
}


def label(code) -> str:
    """Readable Indonesian text for a code; humanised fallback, never raises, None/'' -> ''."""
    if code is None:
        return ""
    text = str(code).strip()
    if not text:
        return ""
    if text in LABELS:
        return LABELS[text]
    if text.startswith("use:") and len(text) > 4:
        return f"Pakai struk {text[4:]}"
    words = text.replace("_", " ")
    return words[:1].upper() + words[1:]


def person(store_or_employees, emp_id) -> str:
    """Name for an employee ID (fallback: the ID). Accepts a DataStore or a list of employee dicts."""
    if emp_id is None:
        return ""
    if hasattr(store_or_employees, "employees"):
        employees = store_or_employees.employees
    else:
        employees = store_or_employees or []
    for emp in employees:
        if emp.get("id") == emp_id:
            return emp.get("name") or str(emp_id)
    return str(emp_id)
