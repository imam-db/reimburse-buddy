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
    # office tool names (agent/reimburse_agent/office/tools.py)
    "send_email": "Email",
    "create_calendar_event": "Undangan rapat",
    "create_task": "Tugas",
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
    # login (web/reimburse_web/auth.py)
    "sign_in": "Masuk",
    "email": "Email",
    "password": "Kata sandi",
    "login_subtitle": "Masuk dengan akun kantor Anda.",
    "show_password": "Tampilkan",
    "hide_password": "Sembunyikan",
    "continue": "Lanjut",
    "auth_off": ("Gerbang login mati: belum ada database akun (REIMBURSE_AUTH_DATABASE_URL), jadi semua "
                 "halaman terbuka. Ini hanya untuk pengembangan lokal."),
    "auth_bad_credentials": "Email atau kata sandi salah.",
    "auth_unavailable": "Layanan masuk sedang bermasalah. Coba lagi sebentar lagi.",
    "auth_rate_limited": "Terlalu banyak percobaan. Coba lagi dalam satu menit.",
    # self-approval guard (web/reimburse_web/app.py, plans.py)
    "self_approval_blocked": "Anda tidak dapat menyetujui permintaan/klaim Anda sendiri.",
    "self_approval_note": ("Anda tercatat sebagai pemilik klaim ini, jadi tombol persetujuan dimatikan. "
                           "Minta reviewer lain menyetujuinya."),
    "self_approval_plan_note": ("Anda tercatat sebagai peminta rencana ini, jadi persetujuan dimatikan. "
                                "Anda tetap bisa menolak atau meminta rekan lain menyetujuinya."),
    "self_approval_submitted_note": ("Anda yang mengajukan permintaan ini, jadi tombol Setujui dimatikan."),
    "submitted_by_note": "Permintaan ini dicatat atas nama pengaju (reviewer yang masuk):",
    # the approval gate on the investigation page (app.py)
    "investigation_gate_note": ("Panggilan ini hanya dijalankan jika Anda menekan Setujui. Hanya pencarian "
                                "baca-saja yang berjalan tanpa bertanya; setiap penulisan atau pesan, "
                                "termasuk permintaan bukti ke pemilik klaim, menunggu persetujuan Anda di "
                                "sini. Persetujuan yang tidak dijawab otomatis ditolak."),
    # daily LLM budget (web/reimburse_web/spend.py)
    "budget_exhausted": "Batas biaya harian demo tercapai. Coba lagi besok.",
    "budget_remaining": "Sisa anggaran LLM hari ini",
    "budget_unconfigured": ("Batas biaya LLM belum diatur, jadi tidak ada panggilan model yang dijalankan. "
                            "Isi LLM_DAILY_BUDGET_USD dan harga token di environment."),
    # ui_* : visible text of the employee, manager and nav pages (app.py, upload_flow.py, manager.py)
    # navigation (app.nav_html, app._pending_nav)
    "ui_nav_home": "Beranda",
    "ui_nav_employee": "Karyawan",
    "ui_nav_finance_admin": "Admin finance",
    "ui_nav_intake": "Permintaan baru",
    "ui_nav_triage": "Triage",
    "ui_nav_approvals": "Persetujuan",
    "ui_nav_pending": "Menunggu persetujuan",
    "ui_nav_my_claims": "Klaim saya",
    "ui_nav_submit_claim": "Ajukan klaim",
    "ui_nav_signed_in": "Masuk sebagai",
    "ui_nav_sign_out": "Keluar",
    # employee portal (app.employee_page)
    "ui_portal_title": "Portal karyawan",
    "ui_my_claims": "Klaim saya",
    "ui_messages_from_finance": "Pesan dari finance",
    "ui_no_messages": "Belum ada pesan.",
    "ui_new_claim": "Klaim baru",
    "ui_new_claim_hint": "Unggah semua struk dan boarding pass sekaligus; kami yang menyusun itemnya untuk Anda.",
    "ui_start_new_claim": "Mulai klaim baru",
    "ui_forwarded_mailbox": "Diteruskan ke mailbox reimburse",
    "ui_employee_footer": "Karyawan:",
    "ui_flash_submitted": "Klaim diajukan",
    "ui_flash_file_added": "File ditambahkan",
    # table headers (employee, manager and claim pages)
    "ui_col_id": "ID",
    "ui_col_purpose": "Keperluan",
    "ui_col_trip": "Perjalanan",
    "ui_col_claimed": "Diajukan (Rp)",
    "ui_col_status": "Status",
    "ui_col_claim": "Klaim",
    "ui_col_subject": "Subjek",
    "ui_col_body": "Isi",
    "ui_col_date": "Tanggal",
    "ui_col_attachments": "Lampiran",
    "ui_col_item": "Item",
    "ui_col_files": "Berkas",
    "ui_col_evidence": "Bukti",
    "ui_col_employee": "Karyawan",
    "ui_col_reason": "Alasan",
    # new claim and draft pages (upload_flow.py)
    "ui_draft": "Draf",
    "ui_claim": "Klaim",
    "ui_field_purpose": "Keperluan",
    "ui_field_destination": "Tujuan",
    "ui_field_dates": "Tanggal",
    "ui_field_end_date": "Tanggal selesai",
    "ui_step_trip": "1. Perjalanan",
    "ui_step_drop": "2. Unggah struk dan boarding pass",
    "ui_drop_hint": "Seret foto atau PDF ke sini, atau klik untuk memilih (maks. {max} berkas, 5 MB per berkas)",
    "ui_extraction_note": "Ekstraksi: {engine}. Anda bisa memeriksa dan memperbaiki setiap isian di layar berikutnya.",
    "ui_read_files": "Baca berkas",
    "ui_pick_files": "Pilih file",
    "ui_check_title": "Periksa item Anda",
    "ui_check_note": ("3. Periksa item yang sudah disusun. Isian kuning dibaca dengan keyakinan rendah. "
                      "Apa pun yang Anda ketik atau ubah ditandai {flag} untuk reviewer."),
    "ui_card": "Kartu",
    "ui_no_file_yet": "Tidak ada berkas: tambah setelah dikirim.",
    "ui_move": "Pindah",
    "ui_merge": "Gabung",
    "ui_split": "Pisah",
    "ui_delete": "Hapus",
    "ui_choose": "pilih...",
    "ui_move_to": "pindah ke...",
    "ui_merge_into": "gabung ke...",
    "ui_save_changes": "Simpan perubahan",
    "ui_add_item": "Tambah item tanpa berkas",
    "ui_submit_claim": "Kirim klaim",
    "ui_field_category": "Kategori",
    "ui_field_date": "Tanggal",
    "ui_field_amount": "Nominal",
    "ui_field_merchant": "Merchant",
    "ui_field_flight_no": "Nomor penerbangan",
    "ui_field_nights": "Jumlah malam",
    "ui_could_not_read": "Berkas tidak bisa dibaca:",
    "ui_err_category": "Pilih kategori.",
    "ui_err_date": "Tanggal harus berada dalam rentang perjalanan.",
    "ui_err_amount": "Nominal harus bilangan bulat rupiah, misalnya 1.350.000.",
    "ui_err_flight_no": "Nomor penerbangan wajib diisi untuk penerbangan.",
    "ui_err_nights": "Jumlah malam wajib diisi untuk hotel.",
    "ui_err_no_item": "Tambah minimal satu item.",
    # claim page for the employee (upload_flow.employee_claim)
    "ui_add_file": "Tambah file",
    "ui_status_done": "Beres",
    "ui_needs_action": "Perlu tindakan",
    "ui_finance_note": ("Finance meninjau setiap klaim. Nominal yang tampil adalah yang Anda ajukan; "
                        "finance yang memutuskan apa yang disetujui."),
    "ui_forwarded_note": "{flash}: berkas diteruskan ke {mailbox} dan ditautkan ke itemnya.",
    # manager area (manager.py)
    "ui_no_approval_requests": "Belum ada permintaan persetujuan.",
    # manager approval requests (docs/adr/0003-manager-approval-requests.md)
    "ui_req_pending": "Menunggu jawaban atasan",
    "ui_req_approved": "Disetujui atasan",
    "ui_req_rejected": "Ditolak atasan",
    "ui_req_approve": "Setujui",
    "ui_req_reject": "Tolak",
    "ui_req_note": "Catatan (opsional)",
    "ui_req_explain": ("Jawaban Anda dicatat di jejak audit dan menjadi masukan untuk finance. "
                       "Keputusan akhir klaim tetap di tangan finance."),
    "ui_req_already_answered": "Permintaan ini sudah dijawab.",
    "ui_req_not_saved": "Jawaban belum tersimpan karena jejak audit tidak tersedia. Coba lagi nanti.",
    "ui_req_ask_manager": "Minta persetujuan atasan",
    "ui_req_reason": "Alasan untuk atasan",
    "ui_req_manager": "Atasan",
    "ui_req_no_manager": "Karyawan ini tidak punya atasan di direktori, jadi permintaan tidak bisa dikirim.",
    "ui_req_open_exists": "Masih ada permintaan yang menunggu jawaban atasan untuk item ini.",
    "ui_req_sent": "Permintaan persetujuan dikirim ke atasan",
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
