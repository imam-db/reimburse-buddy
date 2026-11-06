"""Employee Q&A wording: Indonesian category names, files waiting for finance, flight ticket wording.

Synthetic check dicts only (the shape of to_jsonable(check_claim(...)) plus the web layer's `pending_items`).
No database, model or network.
"""

from __future__ import annotations

from reimburse_agent.qa import tools as T
from reimburse_agent.qa.engine import OfflineQAModel


def check(pending=()):
    return {
        "claim_id": "CLM-2026-W001", "decision": "needs_info", "total_claimed": 1000,
        "pending_items": list(pending),
        "items": [
            {"item_id": "IT-1", "category": "hotel", "claimed": 100, "status": "needs_evidence",
             "gaps": ["hotel_over_cap"], "findings": []},
            {"item_id": "IT-3", "category": "flight", "claimed": 100, "status": "needs_evidence",
             "gaps": ["receipt_missing"], "findings": []},
            {"item_id": "IT-4", "category": "meal", "claimed": 100, "status": "needs_evidence",
             "gaps": ["receipt_missing"], "findings": []},
        ],
    }


def answer(chk, lang):
    return OfflineQAModel._check(T.employee_safe_check(chk), lang)


def test_indonesian_answer_uses_indonesian_category_names():
    out = answer(check(), "id")
    assert "(Hotel)" in out and "(Penerbangan)" in out and "(Makan)" in out
    for raw in ("(hotel)", "(flight)", "(meal)"):
        assert raw not in out


def test_english_answer_uses_english_category_names():
    out = answer(check(), "en")
    assert "(Flight)" in out and "(Meal)" in out and "(Penerbangan)" not in out and "(flight)" not in out


def test_pending_file_says_waiting_not_send_receipt():
    out = answer(check(pending=["IT-4"]), "id")
    it4 = next(ln for ln in out.splitlines() if ln.startswith("- IT-4"))
    assert "menunggu verifikasi finance" in it4 and "Kirim kwitansi" not in it4
    it3 = next(ln for ln in out.splitlines() if ln.startswith("- IT-3"))
    assert "Tiket pesawat" in it3 and "Kirim e-ticket" in it3  # not pending: still asks for the ticket
    en = answer(check(pending=["IT-4"]), "en")
    it4_en = next(ln for ln in en.splitlines() if ln.startswith("- IT-4"))
    assert "waiting for finance verification" in it4_en and "Send the receipt" not in it4_en


def test_pending_does_not_hide_other_gaps_on_the_item():
    chk = check(pending=["IT-1"])
    chk["items"][0]["gaps"] = ["hotel_over_cap", "receipt_missing"]
    out = answer(chk, "id")
    it1 = next(ln for ln in out.splitlines() if ln.startswith("- IT-1"))
    assert "plafon hotel" in it1 and "menunggu verifikasi finance" in it1


def test_masked_item_stays_masked_even_when_pending():
    chk = check(pending=["IT-4"])
    chk["items"][2]["gaps"] = ["receipt_missing", "duplicate_suspected"]
    out = answer(chk, "id")
    it4 = next(ln for ln in out.splitlines() if ln.startswith("- IT-4"))
    assert it4.endswith("Sedang ditinjau finance.")
    assert "verifikasi" not in it4 and "duplicate" not in out


def test_pending_ids_do_not_leak_into_the_safe_check():
    safe = T.employee_safe_check(check(pending=["IT-4"]))
    assert "pending_items" not in safe
