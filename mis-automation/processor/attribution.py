"""Profile-specific attribution-code extraction.

The code is the first three characters of an identifier found in a configured UTM field, after removing a
configured prefix. Nothing is guessed: a value that does not satisfy the approved rule is `unresolved`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CODE_RE = re.compile(r"^[0-9A-Z]{3}$")
DEFAULT_IDENTIFIER_REGEX = r"^([0-9A-Z]{3})[0-9A-F]{12}"


@dataclass(frozen=True)
class AttributionRule:
    priority: int
    source_field: str                    # utm_source | utm_medium | utm_campaign | utm_term
    strip_prefixes: tuple[str, ...] = ()
    identifier_regex: str = DEFAULT_IDENTIFIER_REGEX
    organic_values: tuple[str, ...] = ()
    rule_id: int | None = None


@dataclass(frozen=True)
class AttributionResult:
    status: str                          # 'code' (needs partner lookup) | 'organic' | 'unresolved'
    code: str | None = None
    rule_id: int | None = None
    reason: str | None = None


def _strip_prefix(value: str, prefixes: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Returns (remainder, reason). With no prefixes configured the value is used as is.
    With prefixes configured one MUST match (use '' in the list to allow "no prefix")."""
    if not prefixes:
        return value, None
    for p in sorted(prefixes, key=len, reverse=True):
        if p == "":
            return value, None
        if value.upper().startswith(p.upper()):
            return value[len(p):], None
    return None, "prefix_missing"


def extract(utm: dict[str, str | None], rules: list[AttributionRule]) -> AttributionResult:
    last_reason = "no_attribution_value"
    for rule in sorted(rules, key=lambda r: r.priority):
        raw = utm.get(rule.source_field)
        if raw is None or str(raw).strip() == "":
            continue
        value = str(raw).strip()
        if value.upper() in {o.upper() for o in rule.organic_values}:
            return AttributionResult("organic", None, rule.rule_id, "organic_value")
        rest, why = _strip_prefix(value, rule.strip_prefixes)
        if rest is None:
            last_reason = why or "prefix_missing"
            continue
        try:
            m = re.match(rule.identifier_regex, rest.upper())
        except re.error:
            last_reason = "invalid_rule_regex"
            continue
        if not m or not m.groups():
            last_reason = "identifier_shape_mismatch"
            continue
        code = m.group(1).upper()
        if not CODE_RE.match(code):
            last_reason = "invalid_code"
            continue
        return AttributionResult("code", code, rule.rule_id)
    return AttributionResult("unresolved", None, None, last_reason)
