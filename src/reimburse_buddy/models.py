"""Typed result objects. All money is Decimal and is serialised as a string."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum


class ItemStatus(str, Enum):
    OK = "ok"
    NEEDS_EVIDENCE = "needs_evidence"  # the agent should look for evidence or ask a human
    PARTIAL = "partial"  # part of the amount is reimbursable
    REJECTED = "rejected"


class Decision(str, Enum):
    APPROVE = "approve"
    PARTIAL = "partial_approve"
    REJECT = "reject"
    NEEDS_INFO = "needs_info"


@dataclass
class Finding:
    rule: str
    status: ItemStatus
    message: str
    gap: str | None = None  # machine-readable gap code, e.g. "boarding_pass_missing"
    key: str = ""  # message template key (messages.py), used to localise the note
    params: dict = field(default_factory=dict)
    resolved_by: str | None = None  # set when a human decision closed this gap


@dataclass
class ItemResult:
    item_id: str
    category: str
    claimed: Decimal
    approvable: Decimal
    status: ItemStatus
    findings: list[Finding] = field(default_factory=list)
    evidence_used: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    human_decision: str | None = None


@dataclass
class ClaimCheck:
    claim_id: str
    employee_id: str
    total_claimed: Decimal
    total_approvable: Decimal
    total_pending: Decimal  # amount blocked on missing evidence or a human decision
    decision: Decision
    items: list[ItemResult]
    ignored_evidence: list["IgnoredEvidence"] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)  # claim-level, do not block payment


@dataclass
class IgnoredEvidence:
    id: str
    reason: str  # unknown_id | belongs_to_other_employee | memo_unsigned | no_matching_item


def to_jsonable(obj):
    """Convert dataclasses, Decimals and Enums into plain JSON values."""
    if hasattr(obj, "__dataclass_fields__"):
        obj = asdict(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj.quantize(Decimal("1")))
    if isinstance(obj, Enum):
        return obj.value
    return obj


def rupiah(amount: Decimal) -> str:
    """Format as Indonesian Rupiah, e.g. Rp1.450.000."""
    return "Rp" + f"{int(amount.quantize(Decimal('1'))):,}".replace(",", ".")
