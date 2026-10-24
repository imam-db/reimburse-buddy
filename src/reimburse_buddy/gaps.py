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

ALL = [RECEIPT_MISSING, BOARDING_PASS_MISSING, HOTEL_OVER_CAP, HOTEL_NIGHTS_MISMATCH,
       LATE_CHECKOUT_UNJUSTIFIED, DUPLICATE_SUSPECTED, RETURN_FLIGHT_MISSING]
# Not in ALL (the codes SKILL.md documents): SKILL.md is frozen until after judging, so the two receipt
# checks below are documented in the version B prompt addendum instead. They only fire on evidence that
# carries a receipt amount or on several receipts for one item, which version A's data never has.
EVIDENCE_REVIEW = [RECEIPT_AMOUNT_MISMATCH, MULTIPLE_RECEIPTS]
