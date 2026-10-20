"""Deterministic transform: ParsedTable + approved ProfileSpec -> normalized records, quarantine list, footer figures.

No network, no LLM, no database access. Issue records carry row numbers, field names and reason codes only -
never cell values (they may be personal data).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .attribution import extract as extract_attribution
from .file_parser import ParsedTable
from .normalization import apply_transform, blank, clean_place_name, clean_text
from .schema_registry import ProfileSpec
from .validation import validate_record

LEAD_COLUMNS = ("lead_status_raw", "status_reason", "is_doable", "lead_entry_date", "disbursed_date",
                "requested_loan_amount", "approved_amount", "disbursed_amount", "payout_amount", "lead_source",
                "employment_type", "state", "city", "pincode")
UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_term")
PLACE_FIELDS = ("state", "city", "employment_type")
FOOTER_FIGURE = re.compile(r"(?i)^\s*total\s+(.+?)\s*:\s*(-?[\d,]*\.?\d+)\s*$")


@dataclass
class RowIssue:
    row_number: int
    issue_type: str            # a value allowed by ingestion_issues.issue_type
    severity: str
    reason: str                # short machine code, e.g. invalid_value
    field: str | None = None
    external_lead_id: str | None = None


@dataclass
class Record:
    row_number: int
    external_lead_id: str
    fields: dict                       # canonical lead columns (None where absent)
    source_attributes: dict
    utm: dict
    attribution_status: str            # code | organic | unresolved
    attribution_code: str | None
    attribution_rule_id: int | None
    attribution_reason: str | None
    lead_status_code: str


@dataclass
class TransformResult:
    records: list[Record] = field(default_factory=list)
    issues: list[RowIssue] = field(default_factory=list)
    rows_total: int = 0
    rows_blank: int = 0
    rows_footer: int = 0
    rows_quarantined: int = 0
    rows_duplicate_collapsed: int = 0
    footer_figures: dict = field(default_factory=dict)
    footer_mismatches: list[str] = field(default_factory=list)
    unmapped_statuses: dict = field(default_factory=dict)      # status text -> count (status text is not personal)
    ignored_columns: list[str] = field(default_factory=list)


def parse_footer_figures(cells: list[str]) -> dict:
    out = {}
    for c in cells:
        m = FOOTER_FIGURE.match(c or "")
        if m:
            key = re.sub(r"[^a-z0-9]+", "_", m.group(1).lower()).strip("_")
            out[key] = m.group(2).replace(",", "")
    return out


def _is_footer(cells: list[str], rx: re.Pattern | None) -> bool:
    return bool(rx and any(rx.search(c or "") for c in cells))


def transform_table(table: ParsedTable, spec: ProfileSpec, *, report_date: date | None,
                    implies_disbursement: set[str]) -> TransformResult:
    res = TransformResult(rows_total=len(table.rows))
    cols = spec.column_by_key()
    idx = {k: i for i, k in enumerate(table.header_keys)}
    active = [(cols[k], idx[k]) for k in cols if k in idx]
    res.ignored_columns = sorted(c.header for c in cols.values() if c.role == "exclude" and c.key in idx)
    footer_rx = re.compile(spec.footer_row_regex) if spec.footer_row_regex else None
    smap = {k.strip().lower(): v for k, v in spec.status_map.items()}
    id_idx = next(i for c, i in active if c.canonical_field == "external_lead_id")
    seen: dict[str, tuple[int, tuple]] = {}
    conflicting: set[str] = set()
    extra_copies: dict[str, list[int]] = {}

    for rn, cells in zip(table.row_numbers, table.rows):
        if not any(c.strip() for c in cells):
            res.rows_blank += 1
            continue
        ident = cells[id_idx].strip()
        if not ident:
            if _is_footer(cells, footer_rx):
                res.rows_footer += 1
                res.footer_figures.update(parse_footer_figures(cells))
            else:
                res.issues.append(RowIssue(rn, "missing_external_id", "error", "identity_blank"))
                res.rows_quarantined += 1
            continue

        fields: dict = {f: None for f in LEAD_COLUMNS}
        utm: dict = {u: None for u in UTM_FIELDS}
        extras: dict = {}
        prio: dict = {}
        bad_field = None
        # `only_if_disbursed` needs the normalized status first
        raw_status = None
        for c, i in active:
            if c.canonical_field == "lead_status_raw":
                raw_status = clean_text(cells[i])
        if spec.implied_status_code:
            status_code = spec.implied_status_code
        elif raw_status is None:
            status_code = "unmapped"
        else:
            status_code = smap.get(raw_status.lower(), "unmapped")
            if status_code == "unmapped":
                res.unmapped_statuses[raw_status] = res.unmapped_statuses.get(raw_status, 0) + 1
        counts_as_disbursed = status_code in implies_disbursement

        for c, i in active:
            if c.role == "exclude":
                continue
            raw = cells[i]
            try:
                val = apply_transform(raw, c.transform, c.date_format)
                if c.transform == "number" and val is not None and val < 0:
                    raise ValueError("negative amount")          # money columns are never negative
            except ValueError:
                if blank(raw):
                    val = None
                else:
                    bad_field = c.canonical_field or c.extra_key or c.key
                    break
            if c.role == "extra":
                if val is not None:
                    extras[c.extra_key] = val.isoformat() if isinstance(val, date) else str(val)
                continue
            f = c.canonical_field
            if f == "external_lead_id":
                continue
            if c.only_if_disbursed and not counts_as_disbursed:
                if val is not None:                      # keep, but parked: never lost, never counted as a disbursal
                    extras[c.extra_key] = val.isoformat() if isinstance(val, date) else str(val)
                continue
            if f in UTM_FIELDS:
                if val is not None and (utm[f] is None or c.alias_priority < prio.get(f, 99)):
                    utm[f] = val
                    prio[f] = c.alias_priority
                continue
            if val is None:
                continue
            if f in PLACE_FIELDS:
                val = clean_place_name(val)
            if f == "pincode" and not re.fullmatch(r"[0-9]{6}", str(val)):
                res.issues.append(RowIssue(rn, "validation_failed", "warning", "invalid_pincode", "pincode", ident))
                continue
            if f in fields and (fields[f] is None or c.alias_priority < prio.get(f, 99)):
                fields[f] = val
                prio[f] = c.alias_priority

        if bad_field:
            res.issues.append(RowIssue(rn, "validation_failed", "error", "invalid_value", bad_field, ident))
            res.rows_quarantined += 1
            continue

        violations = validate_record({**fields, "external_lead_id": ident, **{u: utm[u] for u in UTM_FIELDS}},
                                     spec.validation_rules, report_date)
        quarantine = False
        for v in violations:
            sev = "error" if v.on_fail == "quarantine_row" else "warning"
            res.issues.append(RowIssue(rn, "validation_failed", sev, f"{v.rule_type}", v.field, ident))
            quarantine = quarantine or v.on_fail == "quarantine_row"
        if quarantine:
            res.rows_quarantined += 1
            continue

        if status_code == "unmapped" and raw_status is not None:
            res.issues.append(RowIssue(rn, "unmapped_status", "warning", "status_not_in_profile", "lead_status_raw", ident))
        att = extract_attribution(utm, list(spec.attribution_rules))
        rec = Record(rn, ident, fields, extras, utm, att.status, att.code, att.rule_id, att.reason, status_code)

        sig = (tuple(sorted((k, str(v)) for k, v in fields.items())), att.code, status_code,
               tuple(sorted(extras.items())))
        if ident in seen:
            if seen[ident][1] == sig:
                res.rows_duplicate_collapsed += 1
                res.issues.append(RowIssue(rn, "duplicate_in_file", "info", "identical_duplicate", None, ident))
            else:
                conflicting.add(ident)
                extra_copies.setdefault(ident, []).append(rn)
            continue
        seen[ident] = (len(res.records), sig)
        res.records.append(rec)

    if conflicting:                       # same id, different content: ordering is unknowable -> quarantine all copies
        kept = []
        for r in res.records:
            if r.external_lead_id in conflicting:
                res.issues.append(RowIssue(r.row_number, "duplicate_in_file", "error", "conflicting_duplicate",
                                           None, r.external_lead_id))
                res.rows_quarantined += 1
                for extra_rn in extra_copies.get(r.external_lead_id, []):
                    res.issues.append(RowIssue(extra_rn, "duplicate_in_file", "error", "conflicting_duplicate",
                                               None, r.external_lead_id))
                    res.rows_quarantined += 1
            else:
                kept.append(r)
        res.records = kept
        res.issues.append(RowIssue(0, "duplicate_in_file", "error", f"conflicting_ids:{len(conflicting)}"))

    _reconcile_footer(res, implies_disbursement)
    return res


def _reconcile_footer(res: TransformResult, implies: set[str]) -> None:
    f = res.footer_figures
    if not f:
        return
    disb = [r for r in res.records if r.lead_status_code in implies]
    checks = []
    if "disbursed_count" in f:
        checks.append(("disbursed_count", Decimal(f["disbursed_count"]), Decimal(len(disb))))
    if "disbursed_amount" in f:
        checks.append(("disbursed_amount", Decimal(f["disbursed_amount"]),
                       sum((r.fields["disbursed_amount"] or Decimal(0) for r in disb), Decimal(0))))
    if "payout" in f:
        checks.append(("payout", Decimal(f["payout"]),
                       sum((r.fields["payout_amount"] or Decimal(0) for r in disb), Decimal(0))))
    if "doable_count" in f:
        checks.append(("doable_count", Decimal(f["doable_count"]),
                       Decimal(sum(1 for r in res.records if r.fields["is_doable"] is True))))
    for name, declared, actual in checks:
        if abs(declared - actual) > Decimal("0.01") and res.rows_quarantined == 0:
            res.footer_mismatches.append(name)
