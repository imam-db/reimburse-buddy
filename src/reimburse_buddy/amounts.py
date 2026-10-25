"""Strict Rupiah parsing and receipt-attachment normalisation, applied once where evidence enters
the system (mailbox load, scripted reply delivery, web upload forward)."""

from __future__ import annotations

import re

_PREFIX = re.compile(r"^(?:rp|idr)\.?\s*", re.I)
_GROUPED = re.compile(r"^\d{1,3}(?:\.\d{3})+$")  # 1.350.000 (dot = thousands separator in IDR)
_PLAIN = re.compile(r"^\d+$")
_CENTS = re.compile(r"(?:,00|,-)$")
# Money under any other key is not read; its presence makes the receipt unreadable (needs review).
OTHER_MONEY_KEYS = frozenset({"total", "grand_total", "subtotal", "price", "nominal", "amount_text", "jumlah", "total_amount"})


def parse_idr(value) -> int:
    """Whole Rupiah from an int or a string like 'Rp 120.000', '230.000', '120000', '120.000,00'.
    Anything ambiguous ('120,000', '12.5', '1.20.000', floats, <= 0) raises ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"amount {value!r} is not a Rupiah amount")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"amount {value!r} must be positive")
        return value
    if not isinstance(value, str):
        raise ValueError(f"amount {value!r} must be an integer or an IDR string")
    s = _CENTS.sub("", _PREFIX.sub("", value.strip())).strip()
    if _GROUPED.match(s):
        n = int(s.replace(".", ""))
    elif _PLAIN.match(s):
        n = int(s)
    else:
        raise ValueError(f"amount {value!r} is ambiguous; expected e.g. 120000 or Rp120.000")
    if n <= 0:
        raise ValueError(f"amount {value!r} must be positive")
    return n


def normalize_attachment(a: dict) -> dict:
    """Receipts: `amount` becomes an int; a bad amount or money under another key sets `amount_error`.
    Other attachment types pass through unchanged."""
    if not isinstance(a, dict) or a.get("type") != "receipt":
        return a
    a = dict(a)
    other = sorted(k for k in a if k in OTHER_MONEY_KEYS)
    if other:
        a["amount_error"] = f"money under unsupported field(s) {other}; only an integer `amount` is read"
    if "amount" in a and a["amount"] is not None:
        try:
            a["amount"] = parse_idr(a["amount"])
        except ValueError as exc:
            a["amount_error"] = str(exc)
            a.pop("amount")
    return a


def normalize_message(m: dict) -> dict:
    if not isinstance(m, dict) or not m.get("attachments"):
        return m
    return dict(m, attachments=[normalize_attachment(a) for a in m["attachments"]])
