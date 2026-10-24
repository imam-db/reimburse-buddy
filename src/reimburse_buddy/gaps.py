"""Gap codes returned by check_claim. The agent maps each code to a tool (see SKILL.md)."""

RECEIPT_MISSING = "receipt_missing"
BOARDING_PASS_MISSING = "boarding_pass_missing"
HOTEL_OVER_CAP = "hotel_over_cap"
HOTEL_NIGHTS_MISMATCH = "hotel_nights_mismatch"
LATE_CHECKOUT_UNJUSTIFIED = "late_checkout_unjustified"
DUPLICATE_SUSPECTED = "duplicate_suspected"
RETURN_FLIGHT_MISSING = "return_flight_missing"  # claim-level warning

ALL = [RECEIPT_MISSING, BOARDING_PASS_MISSING, HOTEL_OVER_CAP, HOTEL_NIGHTS_MISMATCH,
       LATE_CHECKOUT_UNJUSTIFIED, DUPLICATE_SUSPECTED, RETURN_FLIGHT_MISSING]
