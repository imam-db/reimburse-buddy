"""Gap codes returned by check_claim. The agent maps each code to a tool (see SKILL.md)."""

RECEIPT_MISSING = "receipt_missing"
BOARDING_PASS_MISSING = "boarding_pass_missing"
HOTEL_OVER_CAP = "hotel_over_cap"
HOTEL_NIGHTS_MISMATCH = "hotel_nights_mismatch"
LATE_CHECKOUT_UNJUSTIFIED = "late_checkout_unjustified"
DUPLICATE_SUSPECTED = "duplicate_suspected"
RETURN_FLIGHT_MISSING = "return_flight_missing"  # claim-level warning
RECEIPT_AMOUNT_MISMATCH = "receipt_amount_mismatch"  # receipt amount differs from the item
MULTIPLE_RECEIPTS = "multiple_receipts"  # two or more receipts submitted for one item

RECEIPT_SHARED = "receipt_shared"  # one receipt attached to two or more items

# B_ONLY: emitted by the receipt-amount checks (version B / web / eval data). Version A's data never
# triggers them. SKILL.md is frozen until judging results are out, so it doesn't document them yet;
# merge B_ONLY into the documented set (and the SKILL.md test) once it is unfrozen.
B_ONLY = [RECEIPT_AMOUNT_MISMATCH, MULTIPLE_RECEIPTS, RECEIPT_SHARED]
ALL = [RECEIPT_MISSING, BOARDING_PASS_MISSING, HOTEL_OVER_CAP, HOTEL_NIGHTS_MISMATCH,
       LATE_CHECKOUT_UNJUSTIFIED, DUPLICATE_SUSPECTED, RETURN_FLIGHT_MISSING, *B_ONLY]
