"""Deterministic value normalisation: headers, dates, money, booleans, text, PII guard."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTHS.update({"sept": 9})
MONEY_MAX = Decimal("999999999999.99")          # numeric(14,2)


def normalize_header(h: str) -> str:
    """Same normalisation as mapping_profile_columns.header_key (lower-case, collapsed whitespace)."""
    h = (h or "").replace("﻿", "").replace(" ", " ")
    return re.sub(r"\s+", " ", h.strip()).lower()


def header_signature(headers) -> str:
    keys = sorted({normalize_header(h) for h in headers if normalize_header(h)})
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def blank(v) -> bool:
    return v is None or str(v).strip() == ""


def clean_text(v) -> str | None:
    if blank(v):
        return None
    return re.sub(r"\s+", " ", str(v).replace(" ", " ")).strip()


def parse_decimal(v) -> Decimal | None:
    """'1,23,456.50', '₹ 5000', 'Rs.5,000/-', '(250)', '12.0' -> Decimal(2dp). Blank -> None. Garbage -> ValueError."""
    if blank(v):
        return None
    s = str(v).strip()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = re.sub(r"(?i)\b(inr|rs\.?|rupees?)\b|₹|/-", "", s)
    s = s.replace(",", "").replace(" ", "")
    if s.endswith("-") and not s.startswith("-"):
        neg, s = True, s[:-1]
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?|[+-]?\.\d+", s):
        raise ValueError("not a number")
    try:
        d = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError("not a number") from exc
    if neg:
        d = -d
    d = d.quantize(Decimal("0.01"))
    if abs(d) > MONEY_MAX:
        raise ValueError("amount out of range")
    return d


def parse_date(v, fmt: str) -> date | None:
    """fmt: 'YYYY-MM-DD' | 'DD Mon YYYY' (unpadded day, 'Sept' tolerated) | 'YYYY-MM-DD HH24:MI:SS'."""
    if blank(v):
        return None
    s = str(v).strip()
    try:
        if fmt in ("YYYY-MM-DD", "YYYY-MM-DD HH24:MI:SS"):
            m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?", s)
            if not m:
                raise ValueError
            if fmt == "YYYY-MM-DD" and m.group(4) is not None:
                # tolerate a midnight/time suffix that Excel exports add, but never silently change a day
                pass
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif fmt == "DD Mon YYYY":
            m = re.fullmatch(r"(\d{1,2})[ -]([A-Za-z]{3,4})[ -](\d{4})", s)
            if not m or m.group(2).lower() not in _MONTHS:
                raise ValueError
            d = date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        else:
            raise ValueError
    except ValueError as exc:
        raise ValueError("invalid date") from exc
    if not (2000 <= d.year <= 2100):
        raise ValueError("implausible date")
    return d


def parse_yes_no(v) -> bool | None:
    if blank(v):
        return None
    s = str(v).strip().lower()
    if s in {"yes", "y", "true", "1"}:
        return True
    if s in {"no", "n", "false", "0"}:
        return False
    raise ValueError("not a yes/no value")


def redact_digit_runs(v) -> str | None:
    """Mask payment-reference-like numbers (>= 9 consecutive digits) inside free text."""
    s = clean_text(v)
    return None if s is None else re.sub(r"\d{9,}", "[redacted]", s)


def apply_transform(value, transform: str, date_format: str | None = None):
    if transform == "number":
        return parse_decimal(value)
    if transform == "date":
        return parse_date(value, date_format or "YYYY-MM-DD")
    if transform == "yes_no_boolean":
        return parse_yes_no(value)
    if transform == "upper":
        s = clean_text(value)
        return s.upper() if s else None
    if transform == "redact_digit_runs":
        return redact_digit_runs(value)
    if transform == "text":
        return clean_text(value)
    raise ValueError(f"unknown transform {transform!r}")


def clean_place_name(v) -> str | None:
    """State/city/employment spellings differ by case and spacing across portals ('KERALA', 'Uttarpradesh ')."""
    s = clean_text(v)
    return s.title() if s else None


# ---- PII safety net ---------------------------------------------------------------------------
_PII_PATTERNS = [
    re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"),                 # Indian mobile
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                               # e-mail
    re.compile(r"(?i)\b[A-Z]{5}\d{4}[A-Z]\b"),                             # PAN
    re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)"),                      # Aadhaar-like 12 digits
]
SENSITIVE_HEADER = re.compile(
    r"(?i)(^|[^a-z])(name|phone|mobile|contact|email|e-mail|dob|birth|aadhaar|aadhar|pan|passport|address|"
    r"account|ifsc|upi|bank|statement|password|otp|ssn|customer ?id|agent|reference)([^a-z]|$)")


def looks_sensitive(value) -> bool:
    if blank(value):
        return False
    s = str(value)
    return any(p.search(s) for p in _PII_PATTERNS)


def mask_shape(value, keep: int = 0, limit: int = 24) -> str:
    """Digits -> 9, letters -> a. Used for schema discovery so no real value leaves the process."""
    s = "" if value is None else str(value)
    out = re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", s))
    return out[:limit]
