"""request_missing_evidence: a fixed-template evidence request that can only reach the claim owner.

The caller picks (claim_id, item_id, gap_code); it cannot supply text, a recipient, or numbers.
The gap code must be one check_claim still reports for that item given the evidence and reviewer
decisions passed in, so a request can't be sent for a gap that is already resolved. While a request
for the same (claim, item, gap) is unanswered, asking again returns it instead of resending; once
the employee replied, a new round may be sent (the agent gates that). Registered on the MCP server
only when REIMBURSE_ENABLE_EVIDENCE_TOOL=1 (version B); WorkBuddy's server doesn't expose it.
Messages carry no amounts and no decision; they only ask for a document or an explanation.
"""

from __future__ import annotations

import re

from . import gaps
from .models import to_jsonable
from .rules import check_claim

# gap_code -> (what we ask for in Indonesian, in English). Keywords (receipt/struk, boarding pass,
# hotel, memo, ...) also let the demo's scripted replies match, exactly as a hand-written ask would.
ASKS = {
    gaps.RECEIPT_MISSING: ("struk/kuitansi (receipt)", "the receipt"),
    gaps.BOARDING_PASS_MISSING: ("boarding pass", "the boarding pass"),
    gaps.HOTEL_OVER_CAP: ("memo persetujuan atau alasan tarif hotel di atas batas",
                          "an approval memo or the reason the hotel rate is above the cap"),
    gaps.HOTEL_NIGHTS_MISMATCH: ("bukti jumlah malam menginap hotel (folio)",
                                 "proof of the number of hotel nights (folio)"),
    gaps.LATE_CHECKOUT_UNJUSTIFIED: ("alasan/bukti late checkout (misalnya jadwal penerbangan)",
                                     "the reason or proof for the late checkout (e.g. flight time)"),
    gaps.DUPLICATE_SUSPECTED: ("penjelasan apakah item ini berbeda dari item serupa (bukti terpisah)",
                               "an explanation of whether this item differs from a similar one (separate proof)"),
}

SUBJECT = "Claim {claim_id}: {item_id} - dokumen diperlukan / document needed"
BODY = ("Halo {name},\n\n"
        "Untuk klaim {claim_id}, item {item_id} ({category}), mohon kirimkan {ask_id} dengan membalas email ini.\n\n"
        "For claim {claim_id}, item {item_id} ({category}), please send {ask_en} by replying to this email.\n\n"
        "Terima kasih / Thank you,\nReimbursement team")


NAME_OK = re.compile(r"[A-Za-z][A-Za-z .'\-]{0,59}")


def request_missing_evidence(store, claim_id: str, item_id: str, gap_code: str,
                             evidence_ids: list[str] | None = None,
                             human_decisions: dict[str, str] | None = None) -> dict:
    claim = store.get_claim(claim_id)  # KeyError on unknown claim
    item = next((i for i in claim["items"] if i["id"] == item_id), None)
    if item is None:
        raise KeyError(f"Unknown item {item_id} in {claim_id}")
    if gap_code not in ASKS:
        raise ValueError(f"gap_code must be one of {sorted(ASKS)}")
    category = item.get("category", "")
    if category not in store.policy.get("allowed_categories", []):
        raise ValueError(f"item category {category!r} is not an allowed policy category")
    result = to_jsonable(check_claim(store, claim_id, evidence_ids, human_decisions))
    row = next((i for i in result["items"] if i["item_id"] == item_id), {})
    # open = reported by a finding the reviewer hasn't resolved (gaps[] keeps resolved ones too)
    current = [f["gap"] for f in row.get("findings", []) if f.get("gap") and not f.get("resolved_by")]
    if gap_code not in current:
        raise ValueError(f"check_claim does not report {gap_code} for {claim_id}/{item_id} with the given "
                         f"evidence and decisions (current: {current})")

    owner = claim["employee_id"]
    marker = [claim_id, item_id, gap_code]
    answered = {m.get("in_reply_to") for m in store.inbox if m.get("in_reply_to")}
    prior = [m for m in store.outbox if m.get("evidence_request") == marker]
    if prior and prior[-1]["id"] not in answered:
        return {"status": "already_requested", "message_id": prior[-1]["id"], "to": owner}

    employee = store.get_employee(owner)
    name = str(employee.get("name", ""))
    if not NAME_OK.fullmatch(name):
        raise ValueError(f"employee name for {owner} has an unexpected format; not templating it")
    ask_id, ask_en = ASKS[gap_code]
    fields = {"claim_id": claim_id, "item_id": item_id, "category": category,
              "name": name, "ask_id": ask_id, "ask_en": ask_en}
    sent = store.send_message(owner, BODY.format(**fields), SUBJECT.format(**fields), claim_id,
                              evidence_request=marker)
    return {**sent, "to": owner, "gap_code": gap_code, "round": len(prior) + 1}
