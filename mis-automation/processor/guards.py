"""Semantic guards: rules a mapping must satisfy no matter who or what proposed it (human, alias dictionary, LLM).

`block`  - the mapping is wrong or unsafe; it can never be approved (submit a corrected version).
`review` - the mapping may be right but encodes a business decision; approval must accept it by name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .normalization import SENSITIVE_HEADER, normalize_header
from .schema_registry import ProfileSpec

LOAN_REQUEST = re.compile(r"\b(loan|requested?)\b")
APPROVAL = re.compile(r"approv|sanction")
DISBURSE = re.compile(r"disburs")
NET = re.compile(r"\bnet\b")
LEADLIKE = re.compile(r"lead\s*_?id|^id$|leadid")


@dataclass(frozen=True)
class Finding:
    level: str            # block | review
    code: str
    header: str | None
    message: str


def semantic_guards(spec: ProfileSpec) -> list[Finding]:
    out: list[Finding] = []
    for c in spec.columns:
        h = normalize_header(c.header)
        f = c.canonical_field
        if c.role in ("canonical", "extra") and SENSITIVE_HEADER.search(c.header) and f not in ("external_lead_id",):
            out.append(Finding("block", "sensitive_column_stored", c.header,
                               "header looks like personal/identity data; it must be excluded, not stored"))
        if f == "disbursed_amount":
            if LOAN_REQUEST.search(h) and not DISBURSE.search(h):
                out.append(Finding("block", "loan_amount_not_disbursed", c.header,
                                   "a requested/loan amount is not a disbursed amount"))
            if APPROVAL.search(h):
                out.append(Finding("block", "approved_not_disbursed", c.header,
                                   "an approved/sanctioned amount is not a disbursed amount"))
            if NET.search(h):
                out.append(Finding("review", "net_disbursed_amount_semantics", c.header,
                                   "a NET disbursed amount is not the gross amount; accept only with an approved business rule"))
        if f == "approved_amount" and DISBURSE.search(h):
            out.append(Finding("block", "disbursed_not_approved", c.header, "a disbursed amount is not an approved amount"))
        if f == "requested_loan_amount" and (DISBURSE.search(h) or APPROVAL.search(h)):
            out.append(Finding("block", "approved_or_disbursed_not_requested", c.header, "not a requested amount"))
        if f == "external_lead_id":
            if "application" in h and spec.identity_namespace == "lead_id":
                out.append(Finding("block", "identity_namespace_mismatch", c.header,
                                   "an application number is a different identifier namespace from lead id"))
            if spec.identity_namespace != "lead_id":
                out.append(Finding("review", "identity_namespace_unconfirmed", c.header,
                                   f"namespace '{spec.identity_namespace}' has no verified crosswalk to existing leads; "
                                   "unmatched rows are quarantined"))
                if spec.on_unknown_lead == "create":
                    out.append(Finding("review", "new_namespace_creates_leads", c.header,
                                       "this profile would create leads under a new identifier namespace"))
    if not any(c.canonical_field == "external_lead_id" for c in spec.columns):
        out.append(Finding("block", "no_identity_column", None, "no column maps to the lead identifier"))
    utm_fields = {c.canonical_field for c in spec.columns if c.role == "canonical" and c.canonical_field}
    for r in spec.attribution_rules:
        if r.source_field not in utm_fields:
            out.append(Finding("block", "attribution_source_not_mapped", r.source_field,
                               "attribution rule reads a UTM field no column provides"))
    if spec.import_mode == "enrichment" and not spec.implied_status_code and \
            not any(c.canonical_field == "lead_status_raw" for c in spec.columns):
        out.append(Finding("review", "enrichment_without_status", None, "partial report carries no status"))
    return out


def blockers(spec: ProfileSpec) -> list[Finding]:
    return [f for f in semantic_guards(spec) if f.level == "block"]


def required_acceptances(spec: ProfileSpec) -> set[str]:
    return {f.code for f in semantic_guards(spec) if f.level == "review"} | set(spec.review_flags)
