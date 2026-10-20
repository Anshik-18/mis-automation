"""Applies one Drive file event: parse -> select approved profile -> normalise -> bulk-load -> mark processed.

Ordering guarantees
  * Everything that changes data (source_files, leads, evidence, issues) AND the event's final state commit in ONE
    transaction. A file is 'processed' only if its data committed.
  * One writer per portal at a time (advisory lock), so concurrent files cannot deadlock on shared rows.
  * Parsing/normalising happens before the transaction opens, so locks are held only for the bulk SQL.
Stale-order protection is enforced by the database trigger `leads_merge_guard`; this module adds the one thing
a trigger cannot know: when two DIFFERENT files carry the same freshness stamp and disagree, ordering is
unknowable, so the disagreeing group is quarantined rather than guessed.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import events
from .config import Settings
from .database import Database
from .file_parser import FileFormatError, ParsedTable, read_table
from .pipeline import Record, RowIssue, TransformResult, transform_table
from .schema_registry import ProfileSpec, check_compat, load_active_specs

PORTAL_LOCK_NS = 726_002
ISSUE_CAP = 200                          # stored rows per (issue_type, reason); the rest are summarised in one row

LEAD_VALUE_COLS = ["lead_status_raw", "status_reason", "is_doable", "lead_entry_date", "disbursed_date",
                   "requested_loan_amount", "approved_amount", "disbursed_amount", "payout_amount", "lead_source",
                   "employment_type", "state", "city", "pincode"]
HASH_COLS = ["lead_status_code", *LEAD_VALUE_COLS, "source_attributes", "attribution_status", "attribution_code",
             "attribution_partner_id"]
STAGE_DDL = """CREATE TEMP TABLE stage (
  ord int, row_number int, external_lead_id text, status_code text, lead_status_raw text, status_reason text,
  is_doable boolean, lead_entry_date date, disbursed_date date, requested_loan_amount numeric(14,2),
  approved_amount numeric(14,2), disbursed_amount numeric(14,2), payout_amount numeric(14,2), lead_source text,
  employment_type text, state text, city text, pincode text, source_attributes jsonb, attr_status text,
  attr_code text, partner_id int, rule_id int, utm_source text, utm_medium text, utm_campaign text, utm_term text,
  tie_status boolean DEFAULT false, tie_disb boolean DEFAULT false) ON COMMIT DROP"""
STAGE_COLS = ("ord, row_number, external_lead_id, status_code, lead_status_raw, status_reason, is_doable, "
              "lead_entry_date, disbursed_date, requested_loan_amount, approved_amount, disbursed_amount, "
              "payout_amount, lead_source, employment_type, state, city, pincode, source_attributes, attr_status, "
              "attr_code, partner_id, rule_id, utm_source, utm_medium, utm_campaign, utm_term")


def _hash(alias: str) -> str:
    return "md5(row(" + ", ".join(f"{alias}.{c}" for c in HASH_COLS) + ")::text)"


def _write_predicate() -> str:
    """Conservative 'this row might change' test, so unchanged rows are not rewritten."""
    parts = [":obs > b.last_observed_at",
             "(s.status_code <> 'unmapped' AND :obs > b.status_observed_at)",
             "((s.disbursed_amount IS NOT NULL OR s.disbursed_date IS NOT NULL OR s.payout_amount IS NOT NULL)"
             " AND :obs > b.disbursement_observed_at)",
             "(s.status_code <> 'unmapped' AND :obs >= b.status_observed_at AND "
             "(s.status_code, s.lead_status_raw) IS DISTINCT FROM (l.lead_status_code, l.lead_status_raw))",
             "NOT (l.source_attributes @> s.source_attributes)",
             "(l.attribution_status = 'unresolved' AND s.attr_status <> 'unresolved')",
             "(l.attribution_status = 'unregistered_code' AND s.attr_status = 'resolved' AND s.attr_code = l.attribution_code)",
             "(l.lead_entry_date IS NULL AND s.lead_entry_date IS NOT NULL)"]
    for c in ("status_reason", "is_doable", "disbursed_date", "requested_loan_amount", "approved_amount",
              "disbursed_amount", "payout_amount", "lead_source", "employment_type", "state", "city", "pincode"):
        parts.append(f"(s.{c} IS NOT NULL AND s.{c} IS DISTINCT FROM l.{c})")
    return " OR ".join(parts)


@dataclass
class _Selected:
    spec: ProfileSpec
    table: ParsedTable


def _sanitize_error(exc: BaseException) -> str:
    """Error text safe to store/log: class, SQLSTATE and constraint name - never DETAIL (it can echo row values)."""
    name = type(exc).__name__
    diag = getattr(exc, "diag", None)
    sqlstate = getattr(exc, "sqlstate", None)
    parts = [name]
    if sqlstate:
        parts.append(f"sqlstate={sqlstate}")
    if diag is not None and getattr(diag, "constraint_name", None):
        parts.append(f"constraint={diag.constraint_name}")
    if not sqlstate and not isinstance(exc, (OSError,)):
        msg = re.sub(r"\d{6,}", "[n]", str(exc).splitlines()[0] if str(exc) else "")[:160]
        if msg:
            parts.append(msg)
    return " ".join(parts)


def _stem(name: str) -> str:
    from .filename_parser import split_extension
    return split_extension(name)[0]


def _select(candidates: list[ProfileSpec], data: bytes):
    """Returns ('ok', _Selected, compat) | ('empty', code) | ('unreadable', code) | ('no_fit', details)."""
    tables: dict = {}
    fits, reasons, errors = [], [], []
    for spec in candidates:
        key = (spec.header_row, spec.sheet_name)
        if key not in tables:
            try:
                tables[key] = read_table(data, header_row=spec.header_row, sheet_name=spec.sheet_name)
            except FileFormatError as e:
                tables[key] = e
        t = tables[key]
        if isinstance(t, FileFormatError):
            errors.append(t.code)
            continue
        if t.file_format != spec.file_format:
            reasons.append({"profile_key": spec.profile_key, "reason": "format_mismatch",
                            "expected": spec.file_format, "actual": t.file_format})
            continue
        c = check_compat(spec, t.header_keys)
        if c.level in ("exact", "compatible"):
            fits.append((spec, t, c))
        else:
            reasons.append({"profile_key": spec.profile_key, "reason": "headers_incompatible",
                            "missing_required": list(c.missing_required), "unknown_headers": list(c.unknown_headers)})
    if len(fits) == 1:
        spec, t, c = fits[0]
        return "ok", _Selected(spec, t), c
    if len(fits) > 1:
        return "no_fit", {"reason": "ambiguous_profile_for_headers", "profiles": [f[0].profile_key for f in fits]}
    if errors and not reasons:
        if all(c in ("empty_file", "empty_workbook") for c in errors):
            return "empty", errors[0]
        return "unreadable", errors[0]
    return "no_fit", {"reason": "headers_incompatible" if reasons else "no_active_profile", "candidates": reasons}


def _upsert_source_file(conn, ev: dict, sha: str, size: int, status: str, spec: ProfileSpec | None,
                        error: str | None = None) -> tuple[int, str]:
    row = conn.execute("""
        INSERT INTO mis.source_files
          (portal_id, profile_id, original_filename, drive_file_id, content_sha256, size_bytes, report_date,
           period_start, period_end, observed_at, uploaded_at, processing_status, processing_started_at,
           attempt_count, error_message)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), 1, %s)
        ON CONFLICT (content_sha256) DO UPDATE SET
           processing_status = CASE WHEN mis.source_files.processing_status = 'processed'
                                    THEN mis.source_files.processing_status ELSE EXCLUDED.processing_status END,
           portal_id = COALESCE(EXCLUDED.portal_id, mis.source_files.portal_id),
           profile_id = COALESCE(EXCLUDED.profile_id, mis.source_files.profile_id),
           report_date = COALESCE(EXCLUDED.report_date, mis.source_files.report_date),
           period_start = COALESCE(EXCLUDED.period_start, mis.source_files.period_start),
           period_end = COALESCE(EXCLUDED.period_end, mis.source_files.period_end),
           observed_at = COALESCE(EXCLUDED.observed_at, mis.source_files.observed_at),
           processing_started_at = CASE WHEN mis.source_files.processing_status = 'processed'
                                        THEN mis.source_files.processing_started_at ELSE now() END,
           attempt_count = mis.source_files.attempt_count + CASE WHEN mis.source_files.processing_status = 'processed' THEN 0 ELSE 1 END,
           error_message = CASE WHEN mis.source_files.processing_status = 'processed' THEN NULL ELSE EXCLUDED.error_message END
        RETURNING source_file_id, processing_status""",
        (ev["portal_id"], spec.profile_id if spec else ev["profile_id"], ev["filename"], ev["drive_file_id"], sha,
         size, ev["report_date"], ev["period_start"], ev["period_end"], ev["observed_at"], ev["drive_created_at"],
         status, error)).fetchone()
    return row[0], row[1]


def _insert_issues(conn, source_file_id: int, issues: list[tuple]) -> None:
    """issues: (row_number, issue_type, severity, external_lead_id, message, details_dict). Capped per type/reason."""
    out, seen, over = [], {}, {}
    for it in issues:
        key = (it[1], (it[5] or {}).get("reason"))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] <= ISSUE_CAP:
            out.append(it)
        else:
            over[key] = seen[key]
    for (itype, reason), n in over.items():
        out.append((None, itype, "info", None, "further issues of this kind summarised",
                    {"reason": reason, "total": n, "stored": ISSUE_CAP}))
    if not out:
        return
    with conn.cursor() as cur:
        cur.executemany("""INSERT INTO mis.ingestion_issues
              (source_file_id, row_number, issue_type, severity, external_lead_id, message, details)
              VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                        [(source_file_id, r, t, sv, ext, msg, json.dumps(d or {})) for r, t, sv, ext, msg, d in out])


def _stage_rows(records: list[Record], partner_by_code: dict[str, int]):
    for i, r in enumerate(records):
        f = r.fields
        if r.attribution_status == "code":
            pid = partner_by_code.get(r.attribution_code)
            a_status, a_code = ("resolved" if pid else "unregistered_code"), r.attribution_code
        elif r.attribution_status == "organic":
            pid, a_status, a_code = None, "organic", None
        else:
            pid, a_status, a_code = None, "unresolved", None
        yield (i, r.row_number, r.external_lead_id, r.lead_status_code, f["lead_status_raw"], f["status_reason"],
               f["is_doable"], f["lead_entry_date"], f["disbursed_date"], f["requested_loan_amount"],
               f["approved_amount"], f["disbursed_amount"], f["payout_amount"], f["lead_source"],
               f["employment_type"], f["state"], f["city"], f["pincode"], json.dumps(r.source_attributes),
               a_status, a_code, pid, r.attribution_rule_id, r.utm["utm_source"], r.utm["utm_medium"],
               r.utm["utm_campaign"], r.utm["utm_term"])


def _load(conn, settings: Settings, ev: dict, sel: _Selected, tr: TransformResult, source_file_id: int) -> dict:
    spec = sel.spec
    portal_id, obs, fid = ev["portal_id"], ev["observed_at"], source_file_id
    # partners ---------------------------------------------------------------------------------------
    codes = sorted({r.attribution_code for r in tr.records if r.attribution_code})
    partner_by_code: dict[str, int] = {}
    if codes:
        partner_by_code = dict(conn.execute("SELECT attribution_code, partner_id FROM mis.attribution_partners "
                                            "WHERE attribution_code = ANY(%s)", (codes,)).fetchall())
        if settings.auto_register_partners:
            for c in (c for c in codes if c not in partner_by_code):
                partner_by_code[c] = conn.execute(
                    """INSERT INTO mis.attribution_partners (attribution_code, partner_name, notes)
                       VALUES (%s, %s, 'auto-registered from a report; rename after review')
                       ON CONFLICT (attribution_code) DO UPDATE SET attribution_code = EXCLUDED.attribution_code
                       RETURNING partner_id""", (c, f"Partner {c} (auto)")).fetchone()[0]
    # staging ----------------------------------------------------------------------------------------
    conn.execute(STAGE_DDL)
    with conn.cursor() as cur:
        with cur.copy(f"COPY stage ({STAGE_COLS}) FROM STDIN") as cp:
            for row in _stage_rows(tr.records, partner_by_code):
                cp.write_row(row)
    conn.execute("ANALYZE stage")
    ns = spec.identity_namespace
    params = {"p": portal_id, "ns": ns, "obs": obs, "fid": fid}
    conn.execute(f"""CREATE TEMP TABLE stage_before ON COMMIT DROP AS
        SELECT s.ord, l.lead_id, l.status_observed_at, l.disbursement_observed_at, l.last_observed_at,
               l.last_seen_file_id, l.lead_status_code, l.lead_status_raw, l.disbursed_amount, l.disbursed_date,
               l.payout_amount, l.attribution_status, l.attribution_code, {_hash('l')} AS h
        FROM stage s JOIN mis.leads l ON l.portal_id = %(p)s AND l.id_namespace = %(ns)s
                                      AND l.external_lead_id = s.external_lead_id""", params)
    conn.execute("CREATE UNIQUE INDEX ON stage_before (ord)")
    # equal freshness stamps from a DIFFERENT file that disagree -> order unknowable -> do not apply that group
    ties_status = conn.execute("""
        UPDATE stage s SET tie_status = true FROM stage_before b
        WHERE b.ord = s.ord AND s.status_code <> 'unmapped' AND b.status_observed_at = %(obs)s
          AND b.last_seen_file_id <> %(fid)s
          AND (s.status_code, COALESCE(s.lead_status_raw, '')) IS DISTINCT FROM (b.lead_status_code, COALESCE(b.lead_status_raw, ''))
        RETURNING s.row_number, s.external_lead_id""", params).fetchall()
    ties_disb = conn.execute("""
        UPDATE stage s SET tie_disb = true FROM stage_before b
        WHERE b.ord = s.ord AND b.disbursement_observed_at = %(obs)s AND b.last_seen_file_id <> %(fid)s
          AND (s.disbursed_amount IS NOT NULL OR s.disbursed_date IS NOT NULL OR s.payout_amount IS NOT NULL)
          AND ((s.disbursed_amount IS NOT NULL AND s.disbursed_amount IS DISTINCT FROM b.disbursed_amount)
            OR (s.disbursed_date IS NOT NULL AND s.disbursed_date IS DISTINCT FROM b.disbursed_date)
            OR (s.payout_amount IS NOT NULL AND s.payout_amount IS DISTINCT FROM (SELECT payout_amount FROM mis.leads WHERE lead_id = b.lead_id)))
        RETURNING s.row_number, s.external_lead_id""", params).fetchall()
    conn.execute("""UPDATE stage SET status_code='unmapped', lead_status_raw=NULL, status_reason=NULL,
                           approved_amount=NULL, is_doable=NULL WHERE tie_status""")
    conn.execute("UPDATE stage SET disbursed_amount=NULL, disbursed_date=NULL, payout_amount=NULL WHERE tie_disb")

    inserted_n = 0
    if spec.on_unknown_lead == "create":
        inserted_n = conn.execute("""
            INSERT INTO mis.leads (portal_id, id_namespace, external_lead_id, attribution_status, attribution_code,
               attribution_partner_id, lead_status_code, lead_status_raw, status_reason, is_doable, lead_entry_date,
               disbursed_date, requested_loan_amount, approved_amount, disbursed_amount, payout_amount, lead_source,
               employment_type, state, city, pincode, source_attributes, status_observed_at,
               disbursement_observed_at, last_observed_at, first_seen_file_id, last_seen_file_id)
            SELECT %(p)s, %(ns)s, s.external_lead_id, s.attr_status, s.attr_code, s.partner_id, s.status_code,
                   s.lead_status_raw, s.status_reason, s.is_doable, s.lead_entry_date, s.disbursed_date,
                   s.requested_loan_amount, s.approved_amount, s.disbursed_amount, s.payout_amount, s.lead_source,
                   s.employment_type, s.state, s.city, s.pincode, s.source_attributes, %(obs)s, %(obs)s, %(obs)s,
                   %(fid)s, %(fid)s
            FROM stage s WHERE NOT EXISTS (SELECT 1 FROM stage_before b WHERE b.ord = s.ord)
            ON CONFLICT (portal_id, id_namespace, external_lead_id) DO NOTHING""", params).rowcount
    conn.execute(f"""
        UPDATE mis.leads l SET
           lead_status_code = s.status_code, lead_status_raw = s.lead_status_raw, status_reason = s.status_reason,
           is_doable = s.is_doable, lead_entry_date = s.lead_entry_date, disbursed_date = s.disbursed_date,
           requested_loan_amount = s.requested_loan_amount, approved_amount = s.approved_amount,
           disbursed_amount = s.disbursed_amount, payout_amount = s.payout_amount, lead_source = s.lead_source,
           employment_type = s.employment_type, state = s.state, city = s.city, pincode = s.pincode,
           source_attributes = s.source_attributes, attribution_status = s.attr_status,
           attribution_code = s.attr_code, attribution_partner_id = s.partner_id,
           status_observed_at = %(obs)s, disbursement_observed_at = %(obs)s, last_observed_at = %(obs)s,
           last_seen_file_id = %(fid)s
        FROM stage s JOIN stage_before b ON b.ord = s.ord
        WHERE l.lead_id = b.lead_id AND ({_write_predicate().replace(':obs', '%(obs)s')})""", params)
    res = conn.execute(f"""
        SELECT count(*) FILTER (WHERE b.ord IS NULL AND l.lead_id IS NOT NULL),
               count(*) FILTER (WHERE b.ord IS NOT NULL AND b.h <> {_hash('l')}),
               count(*) FILTER (WHERE b.ord IS NOT NULL AND b.h = {_hash('l')}),
               count(*) FILTER (WHERE l.lead_id IS NULL),
               count(*) FILTER (WHERE b.ord IS NOT NULL AND %(obs)s < b.last_observed_at)
        FROM stage s LEFT JOIN stage_before b ON b.ord = s.ord
        LEFT JOIN mis.leads l ON l.portal_id = %(p)s AND l.id_namespace = %(ns)s AND l.external_lead_id = s.external_lead_id""",
                       params).fetchone()
    ins, upd, unch, unknown, stale = res
    # evidence: first sighting, or attribution changed (e.g. unresolved -> resolved)
    conn.execute("""
        INSERT INTO mis.lead_attribution_evidence (lead_id, source_file_id, profile_id, rule_id, utm_source, utm_medium,
                                                   utm_campaign, utm_term)
        SELECT l.lead_id, %(fid)s, %(pid)s, s.rule_id, s.utm_source, s.utm_medium, s.utm_campaign, s.utm_term
        FROM stage s JOIN mis.leads l ON l.portal_id = %(p)s AND l.id_namespace = %(ns)s AND l.external_lead_id = s.external_lead_id
        LEFT JOIN stage_before b ON b.ord = s.ord
        WHERE b.ord IS NULL OR (b.attribution_status, b.attribution_code) IS DISTINCT FROM (l.attribution_status, l.attribution_code)
        ON CONFLICT (lead_id) DO UPDATE SET source_file_id = EXCLUDED.source_file_id, profile_id = EXCLUDED.profile_id,
           rule_id = EXCLUDED.rule_id, utm_source = EXCLUDED.utm_source, utm_medium = EXCLUDED.utm_medium,
           utm_campaign = EXCLUDED.utm_campaign, utm_term = EXCLUDED.utm_term""", {**params, "pid": spec.profile_id})
    unknown_rows = []
    if unknown:
        unknown_rows = conn.execute("""SELECT s.row_number, s.external_lead_id FROM stage s
              WHERE NOT EXISTS (SELECT 1 FROM mis.leads l WHERE l.portal_id = %s AND l.id_namespace = %s
                                AND l.external_lead_id = s.external_lead_id) ORDER BY s.ord""", (portal_id, ns)).fetchall()
    return {"inserted": ins, "updated": upd, "unchanged": unch, "unknown_lead": unknown, "stale_skipped": stale,
            "ties_status": ties_status, "ties_disb": ties_disb, "unknown_rows": unknown_rows,
            "partners_unregistered": sorted(c for c in codes if c not in partner_by_code)}


def process_event(db: Database, settings: Settings, event_id: int, token: str, data: bytes) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    size = len(data)
    if size > settings.max_upload_mb * 1024 * 1024:
        return _finish_simple(db, event_id, token, "needs_review", reason="file_too_large")
    with db.connection() as conn:                              # --- phase A: read
        ev = events.check_claim(conn, event_id, token)
        if ev["portal_id"] is None or ev["report_date"] is None or ev["observed_at"] is None:
            events.finish(conn, event_id, token, "needs_review", reason="event_not_resolved")
            return {"outcome": "needs_review", "reason": "event_not_resolved", "event_id": event_id}
        stem = _stem(ev["filename"])
        portal_code = conn.execute("SELECT portal_code FROM mis.reporting_portals WHERE portal_id=%s", (ev["portal_id"],)).fetchone()[0]
        pkey = conn.execute("SELECT profile_key FROM mis.mapping_profiles WHERE profile_id=%s", (ev["profile_id"],)).fetchone() \
            if ev["profile_id"] else None
        # filename-matched profiles, plus the profile the event was resolved to (covers manual resolution)
        # a file whose NAME no longer matches any pattern is judged by its content: every active profile of the portal
        # is a candidate and exactly one must fit the headers
        name_flag = (ev.get("review_reason") or "") if (ev.get("review_reason") or "").startswith("filename_pattern_changed") else None
        active = [s for s in load_active_specs(conn, portal_code)
                  if name_flag or re.match(s.filename_regex, stem) or (pkey and s.profile_key == pkey[0])]
        implies = {r[0] for r in conn.execute("SELECT status_code FROM mis.lead_statuses WHERE implies_disbursement")}
    kind, *rest = _select(active, data) if active else ("no_fit", {"reason": "no_active_profile", "candidates": []})
    sf_status = None
    if kind == "unreadable":
        return _finish_simple(db, event_id, token, "needs_review", reason=f"file_unreadable:{rest[0]}")
    if kind in ("empty", "no_fit"):
        sf_status = "empty" if kind == "empty" else "needs_mapping"
    elif not rest[0].table.rows:
        kind, sf_status = "empty", "empty"
    if kind in ("empty", "no_fit"):
        with db.connection() as conn:
            sfid, prev = _upsert_source_file(conn, ev, sha, size, sf_status, None)
            if kind == "empty":
                events.finish(conn, event_id, token, "empty", source_file_id=sfid, reason="no_data_rows")
                return {"outcome": "empty", "event_id": event_id, "source_file_id": sfid}
            detail = rest[0]
            events.finish(conn, event_id, token, "needs_mapping", source_file_id=sfid, reason=detail.get("reason"))
            return {"outcome": "needs_mapping", "event_id": event_id, "source_file_id": sfid,
                    "reason": detail.get("reason"), "detail": detail}
    sel: _Selected = rest[0]
    tr = transform_table(sel.table, sel.spec, report_date=ev["report_date"], implies_disbursement=implies)
    try:                                                       # --- phase C: one transaction
        with db.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (PORTAL_LOCK_NS, ev["portal_id"]))
            sfid, status = _upsert_source_file(conn, ev, sha, size, "processing", sel.spec)
            if status == "processed":
                events.finish(conn, event_id, token, "duplicate_content", source_file_id=sfid, reason="same content already processed")
                return {"outcome": "duplicate_content", "event_id": event_id, "source_file_id": sfid}
            stats = _load(conn, settings, ev, sel, tr, sfid)
            issues = [(i.row_number or None, i.issue_type, i.severity, i.external_lead_id, i.reason,
                       {"reason": i.reason, "field": i.field}) for i in tr.issues]
            for rn, ext in stats["unknown_rows"]:
                issues.append((rn, "unknown_lead_skipped", "warning", ext,
                               "identifier not found for this portal/namespace; quarantined",
                               {"reason": "no_verified_crosswalk" if sel.spec.on_unknown_lead == "quarantine" else "skipped",
                                "namespace": sel.spec.identity_namespace}))
            for rn, ext in stats["ties_status"]:
                issues.append((rn, "stale_update_skipped", "warning", ext, "same freshness stamp, conflicting status",
                               {"reason": "ambiguous_order_tie", "group": "status"}))
            for rn, ext in stats["ties_disb"]:
                issues.append((rn, "stale_update_skipped", "warning", ext, "same freshness stamp, conflicting disbursement",
                               {"reason": "ambiguous_order_tie", "group": "disbursement"}))
            for name in tr.footer_mismatches:
                issues.append((None, "footer_mismatch", "warning", None, "footer total differs from loaded rows",
                               {"reason": name, "declared": tr.footer_figures.get(name)}))
            if stats["stale_skipped"]:
                issues.append((None, "stale_update_skipped", "info", None, "file older than data already applied",
                               {"reason": "older_file", "rows": stats["stale_skipped"]}))
            if tr.unmapped_statuses:
                issues.append((None, "unmapped_status", "warning", None, "statuses without a mapping",
                               {"reason": "summary", "statuses": dict(list(tr.unmapped_statuses.items())[:20])}))
            _insert_issues(conn, sfid, issues)
            quarantined = tr.rows_quarantined + stats["unknown_lead"]
            att = {k: sum(1 for r in tr.records if r.attribution_status == k) for k in ("code", "organic", "unresolved")}
            footer = {"declared": tr.footer_figures, "mismatches": tr.footer_mismatches,
                      "duplicates_collapsed": tr.rows_duplicate_collapsed, "attribution": att,
                      "ties": {"status": len(stats["ties_status"]), "disbursement": len(stats["ties_disb"])},
                      "unregistered_codes": stats["partners_unregistered"]}
            conn.execute("""UPDATE mis.source_files SET processing_status='processed', processed_at=now(),
                  rows_total=%s, rows_data=%s, rows_footer=%s, rows_inserted=%s, rows_updated=%s, rows_unchanged=%s,
                  rows_stale_skipped=%s, rows_quarantined=%s, footer_totals=%s::jsonb, error_message=NULL
                  WHERE source_file_id=%s""",
                         (tr.rows_total, tr.rows_total - tr.rows_blank - tr.rows_footer, tr.rows_footer, stats["inserted"],
                          stats["updated"], stats["unchanged"], stats["stale_skipped"], quarantined,
                          json.dumps(footer), sfid))
            events.finish(conn, event_id, token, "processed", source_file_id=sfid, reason=name_flag)
        return {"outcome": "processed", "event_id": event_id, "source_file_id": sfid, "filename_flag": name_flag,
                "profile_key": sel.spec.profile_key, "profile_version": sel.spec.version,
                "report_date": ev["report_date"].isoformat(),
                "rows": {"total": tr.rows_total, "blank": tr.rows_blank, "footer": tr.rows_footer,
                         "inserted": stats["inserted"], "updated": stats["updated"], "unchanged": stats["unchanged"],
                         "quarantined": quarantined, "stale_skipped": stats["stale_skipped"],
                         "duplicates_collapsed": tr.rows_duplicate_collapsed},
                "footer_mismatches": tr.footer_mismatches, "unregistered_codes": stats["partners_unregistered"],
                "unmapped_statuses": len(tr.unmapped_statuses)}
    except events.LostClaim:
        raise
    except Exception as exc:                                   # rolled back by Database.connection()
        return _record_failure(db, settings, ev, token, sha, size, sel.spec, exc)


def _finish_simple(db: Database, event_id: int, token: str, status: str, *, reason: str) -> dict:
    with db.connection() as conn:
        events.finish(conn, event_id, token, status, reason=reason)
    return {"outcome": status, "event_id": event_id, "reason": reason}


def _record_failure(db: Database, settings: Settings, ev: dict, token: str, sha: str, size: int,
                    spec: ProfileSpec, exc: BaseException) -> dict:
    msg = _sanitize_error(exc)
    with db.connection() as conn:
        try:
            _upsert_source_file(conn, ev, sha, size, "failed", spec, error=msg)
        except Exception:
            pass
        events.finish(conn, ev["event_id"], token, "failed", error=msg)
        conn.execute("""UPDATE mis.drive_file_events SET status='dead' WHERE event_id=%s AND attempt_count >= %s""",
                     (ev["event_id"], settings.max_attempts))
        st = conn.execute("SELECT status, attempt_count FROM mis.drive_file_events WHERE event_id=%s", (ev["event_id"],)).fetchone()
    return {"outcome": "failed", "event_id": ev["event_id"], "error": msg, "event_status": st[0], "attempts": st[1]}
