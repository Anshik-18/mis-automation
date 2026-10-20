"""Row-level validation driven by mapping_profile_validation_rules (closed rule-type allow-list)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class ValidationRule:
    canonical_field: str
    rule_type: str            # required | regex | min_value | max_value | allowed_values | not_after_report_date
    rule_param: dict
    on_fail: str = "quarantine_row"


@dataclass(frozen=True)
class Violation:
    field: str
    rule_type: str
    on_fail: str
    message: str


def validate_record(fields: dict, rules: list[ValidationRule], report_date: date | None) -> list[Violation]:
    out: list[Violation] = []
    for r in rules:
        v = fields.get(r.canonical_field)
        if r.rule_type == "required":
            if v is None or (isinstance(v, str) and not v.strip()):
                out.append(Violation(r.canonical_field, r.rule_type, r.on_fail, "required value missing"))
            continue
        if v is None:
            continue                                  # optional and absent
        p = r.rule_param
        bad = None
        if r.rule_type == "regex":
            if not re.search(p.get("pattern", ""), str(v)):
                bad = "does not match required pattern"
        elif r.rule_type == "min_value":
            if Decimal(str(v)) < Decimal(str(p["value"])):
                bad = f"below minimum {p['value']}"
        elif r.rule_type == "max_value":
            if Decimal(str(v)) > Decimal(str(p["value"])):
                bad = f"above maximum {p['value']}"
        elif r.rule_type == "allowed_values":
            if str(v) not in {str(x) for x in p.get("values", [])}:
                bad = "not an allowed value"
        elif r.rule_type == "not_after_report_date":
            if report_date and isinstance(v, date) and v > report_date + timedelta(days=int(p.get("tolerance_days", 0))):
                bad = "date is after the report date"
        if bad:
            out.append(Violation(r.canonical_field, r.rule_type, r.on_fail, bad))
    return out
