"""AI-assisted mapping discovery for a genuinely new or changed file layout.

Order of authority (each step can only fill what the previous one left open):
  1. the portal's existing profile (same header -> same mapping)       deterministic
  2. the alias dictionary (config/column_aliases.yaml)                 deterministic
  3. local inference (attribution column/prefix, date formats, status keywords, filename regex)   deterministic
  4. the LLM, ONLY for headers still unresolved, ONLY on a redacted description, output validated by JSON Schema
  5. semantic guards demote anything unsafe and turn it into an open question
The result is always a PROPOSED profile; only a named person can activate it. The LLM never runs code.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime

import jsonschema

from . import events
from .attribution import DEFAULT_IDENTIFIER_REGEX, AttributionRule
from .config import Settings
from .database import Database
from .file_parser import FileFormatError, ParsedTable, read_table
from .filename_parser import parse_with_pattern
from .guards import blockers, semantic_guards
from .llm import LLMDisabled, LLMError
from .normalization import SENSITIVE_HEADER, blank, normalize_header
from .profiler import (TableProfile, infer_attribution, llm_view, map_status_values, pick_attribution_column,
                       profile_table, suggest_filename_regex)
from .schema_registry import (ColumnSpec, ProfileSpec, check_compat, ensure_portal, insert_spec, load_aliases,
                              load_spec_from_db, validate_spec)
from .validation import ValidationRule

NUMERIC_FIELDS = {"requested_loan_amount", "approved_amount", "disbursed_amount", "payout_amount"}
DATE_FIELDS = {"lead_entry_date", "disbursed_date"}
UTM = ("utm_source", "utm_medium", "utm_campaign", "utm_term")
IDENT_HEADER = re.compile(r"(lead|application)\s*_?(id|ref|reference|no|number)\b")
ID_NAMESPACE_BY_HEADER = [(re.compile(r"application"), "application_number"), (re.compile(r"lead\s*_?(id|ref|reference|no|number)\b|leadid|^id$"), "lead_id")]

LLM_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["columns", "open_questions"],
    "properties": {
        "columns": {"type": "array", "maxItems": 200, "items": {
            "type": "object", "additionalProperties": False, "required": ["header", "role"],
            "properties": {
                "header": {"type": "string", "maxLength": 120},
                "role": {"enum": ["canonical", "extra", "exclude"]},
                "field": {"type": ["string", "null"], "maxLength": 60},
                "transform": {"enum": ["text", "upper", "number", "date", "yes_no_boolean", "redact_digit_runs"]},
                "date_format": {"enum": ["YYYY-MM-DD", "DD Mon YYYY", "YYYY-MM-DD HH24:MI:SS", None]},
                "extra_key": {"type": ["string", "null"], "pattern": "^[a-z0-9_]{1,40}$"},
                "reason": {"type": "string", "maxLength": 200},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}},
        "open_questions": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False, "required": ["question"],
            "properties": {"header": {"type": ["string", "null"], "maxLength": 120},
                           "question": {"type": "string", "maxLength": 300}}}}}}

SYSTEM = ("You map the columns of a loan-lead report to a fixed set of canonical fields. You receive ONLY header names, "
          "inferred types, statistics and masked value shapes (digits=9, letters=a) - never customer data. "
          "Answer with one JSON object that matches the schema exactly. Rules: use only the listed canonical fields and "
          "transforms; a column you are not sure about must be role 'extra' (non-sensitive) or 'exclude' (anything that "
          "could be personal or identity data); never map a 'loan amount', 'requested amount' or 'approved amount' to "
          "disbursed_amount; never treat a net amount as gross; if two columns could be the lead identifier, an "
          "attribution/UTM field or a money field, do not choose - add an open question instead. No prose outside the JSON.")


PORTAL_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,30}$")
AI_READ_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["looks_like_lead_report", "portal_code", "confidence", "reason"],
    "properties": {"looks_like_lead_report": {"type": "boolean"},
                   "portal_code": {"type": "string", "maxLength": 31},
                   "confidence": {"enum": ["high", "medium", "low"]},
                   "reason": {"type": "string", "maxLength": 240}}}
AI_READ_SYSTEM = ("You look at the NAME and the COLUMN HEADERS (no data) of a file that arrived in a folder of loan-lead reports "
                  "from lending portals. Decide whether it looks like a loan-lead / application / disbursement report and which "
                  "lender or portal it most likely comes from, using only the file name and header names. portal_code must be "
                  "lowercase letters, digits or underscores (3-31 chars, start with a letter), taken from the name; if the name "
                  "gives no clue use 'unknown'. Be honest about confidence. Answer with one JSON object matching the schema; "
                  "no prose outside the JSON.")


def derive_portal_code(filename: str) -> str | None:
    """First word of the file name, e.g. 'NewBank_leads_2026...' -> 'newbank'. None when there is nothing usable."""
    from .filename_parser import split_extension
    m = re.match(r"[A-Za-z][A-Za-z0-9]*", split_extension(filename)[0])
    code = m.group(0).lower() if m else ""
    return code if PORTAL_CODE_RE.match(code) and code not in {"leads", "lead", "report", "reports", "data", "export", "file", "mis"} else None


def ai_read(llm, filename: str, headers: list[str]) -> dict | None:
    """One extra, tiny LLM call for a file nobody recognises. Names only, validated, advisory: any failure => None."""
    if llm is None:
        return None
    from .filename_parser import split_extension
    try:
        raw = llm.complete_json(AI_READ_SYSTEM, json.dumps({"file_name": split_extension(filename)[0][:120],
                                                            "headers": [h[:80] for h in headers[:80]]}))
        jsonschema.validate(raw, AI_READ_SCHEMA)
    except (LLMDisabled, LLMError, jsonschema.ValidationError):
        return None
    out = dict(raw)
    out["reason"] = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f]", " ", out["reason"])).strip()[:200]
    out["portal_code"] = out["portal_code"].strip().lower()
    return out


@dataclass
class Proposal:
    profile_id: int
    profile_key: str
    version: int
    portal_code: str
    reused: bool
    used_llm: bool
    counts: dict
    open_questions: list[dict]
    flags: list[str]
    unmapped_statuses: list[str]
    notification: str


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:n] or "col"


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


def _namespace_for(header: str) -> str | None:
    h = normalize_header(header)
    for rx, ns in ID_NAMESPACE_BY_HEADER:
        if rx.search(h):
            return ns
    return None


def _type_ok(field: str, itype: str) -> bool:
    if field in NUMERIC_FIELDS:
        return itype in ("number", "integer", "empty")
    if field in DATE_FIELDS:
        return itype.startswith("date:") or itype == "empty"
    if field == "is_doable":
        return itype in ("boolean", "text", "empty")
    return True


def build_columns(table: ParsedTable, tp: TableProfile, base: ProfileSpec | None, aliases: dict,
                  canonical: set[str]) -> tuple[list[ColumnSpec | None], list[dict], list[str]]:
    """Deterministic pass. Returns (columns aligned to table headers with None = unresolved, questions, flags)."""
    questions: list[dict] = []
    flags: list[str] = []
    base_cols = base.column_by_key() if base else {}
    out: list[ColumnSpec | None] = []
    for c in tp.columns:
        if c.key in base_cols:
            out.append(base_cols[c.key])
            continue
        if c.sensitive_header and not IDENT_HEADER.search(c.key):
            out.append(ColumnSpec(c.header, "exclude", exclusion_reason="sensitive header: not stored",
                                  sensitivity="personal"))
            continue
        a = aliases.get(c.key)
        if a and a["field"] in canonical:
            f, tr = a["field"], a.get("transform", "text")
            if not _type_ok(f, c.inferred_type):
                questions.append({"header": c.header, "blocking": True, "source": "rule",
                                  "question": f"values do not look like {f} (found {c.inferred_type}); confirm or fix the format"})
                out.append(None)
                continue
            df = None
            if tr == "date":
                df = c.inferred_type.split(":", 1)[1] if c.inferred_type.startswith("date:") else None
                if df is None and c.non_blank:
                    questions.append({"header": c.header, "blocking": True, "source": "rule",
                                      "question": "date format is not one of YYYY-MM-DD, DD Mon YYYY, YYYY-MM-DD HH24:MI:SS"})
                    out.append(None)
                    continue
                df = df or "YYYY-MM-DD"
            if a.get("flag"):
                flags.append(a["flag"])
            out.append(ColumnSpec(c.header, "canonical", canonical_field=f, transform=tr, date_format=df,
                                  sensitivity="financial" if f in NUMERIC_FIELDS else "none"))
            continue
        out.append(None)
    return out, questions, flags


def _apply_llm(unresolved: list[str], tp: TableProfile, resolved_fields: set[str], canonical_desc: dict,
               llm, table_cols: dict[str, "ColumnProfile"]) -> tuple[dict[str, ColumnSpec], list[dict]]:
    user = json.dumps({"canonical_fields": canonical_desc, "already_mapped_fields": sorted(resolved_fields),
                       "unresolved_columns": llm_view(tp, unresolved)}, ensure_ascii=False)
    raw = llm.complete_json(SYSTEM, user)
    jsonschema.validate(raw, LLM_SCHEMA)
    got: dict[str, ColumnSpec] = {}
    qs: list[dict] = []
    seen_fields: dict[str, str] = {}
    for item in raw["columns"]:
        h = item["header"]
        if h not in unresolved or h in got:
            continue                                     # ignore invented/duplicate headers
        col = table_cols[h]
        if (col.sensitive_header and not IDENT_HEADER.search(col.key)) or col.sensitive_values:
            got[h] = ColumnSpec(h, "exclude", exclusion_reason="sensitive: not stored", sensitivity="personal")
            continue
        role, f = item["role"], item.get("field")
        if role == "canonical":
            if f not in canonical_desc or f in resolved_fields or f == "external_lead_id" and False:
                qs.append({"header": h, "blocking": True, "source": "llm",
                           "question": f"suggested field '{f}' is unknown or already mapped; choose the field manually"})
                continue
            if f in seen_fields:
                qs.append({"header": h, "blocking": True, "source": "llm",
                           "question": f"ambiguous: '{seen_fields[f]}' and '{h}' both look like {f}"})
                continue
            tr = item.get("transform", "text")
            if not _type_ok(f, col.inferred_type):
                qs.append({"header": h, "blocking": True, "source": "llm",
                           "question": f"suggested {f} but values look like {col.inferred_type}"})
                continue
            df = item.get("date_format")
            if tr == "date" and (not df or (col.inferred_type.startswith("date:") and df != col.inferred_type[5:])):
                df = col.inferred_type[5:] if col.inferred_type.startswith("date:") else None
            if (tr == "date") != (df is not None):
                qs.append({"header": h, "blocking": True, "source": "llm", "question": "date format unsupported or unclear"})
                continue
            seen_fields[f] = h
            got[h] = ColumnSpec(h, "canonical", canonical_field=f, transform=tr, date_format=df)
        elif role == "extra":
            got[h] = ColumnSpec(h, "extra", extra_key=item.get("extra_key") or _slug(h), transform="text")
        else:
            got[h] = ColumnSpec(h, "exclude", exclusion_reason=(item.get("reason") or "excluded by proposal")[:200])
    for q in raw["open_questions"]:
        qs.append({"header": q.get("header"), "blocking": True, "source": "llm", "question": q["question"][:300]})
    return got, qs


def propose_spec(table: ParsedTable, *, portal_code: str, filename: str, base: ProfileSpec | None, aliases: dict,
                 canonical_desc: dict, status_codes: set[str], llm=None, profile_key: str | None = None
                 ) -> tuple[ProfileSpec, bool, list[str]]:
    """Pure function (no DB): table + context -> (proposed spec, used_llm, unmapped_status_values)."""
    canonical = set(canonical_desc)
    tp = profile_table(table)
    cols_by_header = {c.header: c for c in tp.columns}
    cols, questions, flags = build_columns(table, tp, base, aliases, canonical)
    used_llm = False
    unresolved = [c.header for c, s in zip(tp.columns, cols) if s is None and not any(q["header"] == c.header for q in questions)]
    resolved_fields = {s.canonical_field for s in cols if s and s.role == "canonical" and s.canonical_field}
    if unresolved and llm is not None:
        try:
            got, qs = _apply_llm(unresolved, tp, resolved_fields, canonical_desc, llm, cols_by_header)
            used_llm = True
            questions += qs
            cols = [got.get(c.header, s) if s is None else s for c, s in zip(tp.columns, cols)]
        except LLMDisabled:
            pass
        except (LLMError, jsonschema.ValidationError):
            questions.append({"header": None, "blocking": False, "source": "system",
                              "question": "LLM suggestion unavailable or invalid; unresolved columns were kept as extras/excluded"})
    notes_unmapped = []
    for c, s in zip(tp.columns, cols):                      # conservative fallback for anything still unresolved
        if s is None:
            idx = tp.columns.index(c)
            cols[idx] = ColumnSpec(c.header, "extra", extra_key=_slug(c.header), transform="text")
            notes_unmapped.append(c.header)
    final: list[ColumnSpec] = [s for s in cols if s is not None]

    # identity -----------------------------------------------------------------------------------------
    ident = [c for c in final if c.canonical_field == "external_lead_id"]
    namespace, on_unknown = "lead_id", "create"
    if not ident:
        questions.append({"header": None, "blocking": True, "source": "rule", "question": "no column identifies the lead"})
    elif len(ident) > 1:
        questions.append({"header": None, "blocking": True, "source": "rule",
                          "question": "more than one column could be the lead identifier: " + ", ".join(c.header for c in ident)})
        final = [replace(c, role="extra", canonical_field=None, extra_key=_slug(c.header)) if c in ident else c for c in final]
    else:
        ns = _namespace_for(ident[0].header)
        if ns is None:
            questions.append({"header": ident[0].header, "blocking": True, "source": "rule",
                              "question": "identifier namespace is unknown; confirm what kind of id this is"})
            ns = _slug(ident[0].header)
        namespace = ns
        if ns != "lead_id":
            on_unknown = "quarantine"
    # statuses -----------------------------------------------------------------------------------------
    status_col = next((c for c in final if c.canonical_field == "lead_status_raw"), None)
    status_map, unknown_statuses = {}, []
    report_type, import_mode, implied = "lead_export", "incremental", None
    if status_col:
        i = table.header_keys.index(status_col.key)
        distinct = sorted({r[i].strip() for r in table.rows if not blank(r[i])})
        status_map, unknown_statuses = map_status_values(distinct)
        status_map = {k: v for k, v in status_map.items() if v in status_codes}
    else:
        disb = [c for c in final if c.canonical_field in ("disbursed_amount", "disbursed_date")]
        full = bool(disb) and all(
            all(not blank(r[table.header_keys.index(c.key)]) for r in table.rows) for c in disb) and bool(table.rows)
        if full:
            report_type, import_mode, implied, on_unknown = "disbursed_cases", "enrichment", "disbursed", "quarantine"
            flags.append("report_type_inferred_disbursed_only")
    # disbursement columns on non-disbursed rows (observed in lead exports) are parked, not counted ------
    if status_col and status_map:
        si = table.header_keys.index(status_col.key)
        implied_set = {k for k, v in status_map.items() if v == "disbursed"}
        for idx, c in enumerate(final):
            if c.canonical_field in ("disbursed_amount", "disbursed_date", "payout_amount"):
                ci = table.header_keys.index(c.key)
                off = sum(1 for r in table.rows if not blank(r[ci]) and r[si].strip() not in implied_set)
                if off:
                    final[idx] = replace(c, only_if_disbursed=True, extra_key=f"unconfirmed_{c.canonical_field}")
    # attribution --------------------------------------------------------------------------------------
    utm_cols = {c.header: c.canonical_field for c in final if c.canonical_field in UTM}
    sub = TableProfile(tp.rows, tp.columns, {h: v for h, v in tp.ident_candidates.items() if h in utm_cols})
    col_h, ambiguous, inf = pick_attribution_column(sub)
    rules: list[AttributionRule] = []
    if ambiguous:
        questions.append({"header": None, "blocking": True, "source": "rule",
                          "question": "attribution code could come from more than one column: " + ", ".join(ambiguous)})
    elif col_h:
        pref = (inf["dominant_prefix"],) if inf["prefix_share"] >= 0.9 and inf["dominant_prefix"] else ()
        organic = ()
        ci = table.header_keys.index(normalize_header(col_h))
        present = {r[ci].strip().upper() for r in table.rows if not blank(r[ci])}
        organic = tuple(v for v in ("ORGANIC", "EXISTING") if v in present)
        rules = [AttributionRule(1, utm_cols[col_h], tuple(pref), DEFAULT_IDENTIFIER_REGEX, organic)]
    else:
        notes_unmapped.append("(no attribution column detected)")
    # filename regex / profile identity ----------------------------------------------------------------
    rx, fn_notes = (base.filename_regex, []) if base and re.match(base.filename_regex, _stem(filename)) else suggest_filename_regex(filename)
    rdf = "YYYY-MM-DD" if re.search(r"\\d\{4\}-\\d\{2\}-\\d\{2\}", rx) else "YYYYMMDD"
    if base and re.match(base.filename_regex, _stem(filename)):
        rdf = base.report_date_format
    vr = [ValidationRule("external_lead_id", "required", {})]
    if status_col:
        vr.append(ValidationRule("lead_status_raw", "required", {}))
    for c in final:
        if c.canonical_field in NUMERIC_FIELDS:
            vr.append(ValidationRule(c.canonical_field, "min_value", {"value": 0}))
    if any(c.canonical_field == "lead_entry_date" for c in final):
        vr.append(ValidationRule("lead_entry_date", "not_after_report_date", {"tolerance_days": 1}, "warn"))
    spec = ProfileSpec(
        portal_code=portal_code, profile_key=profile_key or (base.profile_key if base else f"{portal_code}_{_slug(report_type)}"),
        report_type=report_type, import_mode=import_mode, file_format=table.file_format, header_row=table.meta.get("header_row", 1),
        filename_regex=rx, report_date_format=rdf, columns=final, status_map=status_map, attribution_rules=rules,
        validation_rules=vr, identity_namespace=namespace, on_unknown_lead=on_unknown, implied_status_code=implied,
        footer_row_regex=base.footer_row_regex if base else None, layout_family=None,
        notes="proposal; unmapped columns kept as extras: " + ", ".join(notes_unmapped[:30]) if notes_unmapped else "proposal",
        review_flags=sorted(set(flags)), open_questions=[])
    # guards: demote anything unsafe and ask a person ---------------------------------------------------
    for _ in range(3):
        bad = [f for f in blockers(spec) if f.header]
        if not bad:
            break
        hs = {f.header for f in bad}
        for f in bad:
            questions.append({"header": f.header, "blocking": True, "source": "guard", "question": f"{f.code}: {f.message}"})
        spec.columns = [replace(c, role="exclude", canonical_field=None, extra_key=None, transform="text", date_format=None,
                                only_if_disbursed=False, exclusion_reason=f"blocked by guard; needs a decision")
                        if c.header in hs and SENSITIVE_HEADER.search(c.header)
                        else replace(c, role="extra", canonical_field=None, extra_key=_slug(c.header), transform="text",
                                     date_format=None, only_if_disbursed=False) if c.header in hs else c
                        for c in spec.columns]
    spec.open_questions = [q for q in questions if q.get("blocking")]
    spec.review_flags = sorted(set(spec.review_flags) | {f.code for f in semantic_guards(spec) if f.level == "review"})
    return spec, used_llm, unknown_statuses


def _stem(name: str) -> str:
    from .filename_parser import split_extension
    return split_extension(name)[0]


def propose(db: Database, settings: Settings, llm, event_id: int, data: bytes, *, portal_code: str | None = None,
            create_portal: bool = False, display_name: str | None = None, profile_key: str | None = None,
            requested_by: str = "system") -> Proposal:
    aliases = load_aliases(settings.alias_dictionary_path)
    try:
        table = read_table(data)
    except FileFormatError as e:
        raise ValueError(f"file cannot be read: {e.code}") from None
    with db.connection() as conn:
        ev = conn.execute("""SELECT event_id, original_filename, status, portal_id, drive_created_at FROM mis.drive_file_events
                             WHERE event_id=%s""", (event_id,)).fetchone()
        if not ev:
            raise KeyError("event not found")
        if ev[2] not in ("needs_mapping", "needs_review"):
            raise ValueError(f"event is {ev[2]}; only needs_mapping/needs_review events can be proposed for")
        pid_ = ev[3]
        ai = None
        if pid_ is None and not portal_code:
            # a file nobody recognises: work out a likely portal from its name (+ one advisory AI read), parked inactive
            # until a person approves its first mapping
            ai = ai_read(llm, ev[1], table.headers)
            code = ai["portal_code"] if ai and ai["confidence"] != "low" and PORTAL_CODE_RE.match(ai["portal_code"]) \
                and ai["portal_code"] != "unknown" else None
            code = code or derive_portal_code(ev[1])
            if not code:
                raise ValueError("a portal cannot be worked out from this file name; pass 'portal'")
            row = conn.execute("SELECT portal_id FROM mis.reporting_portals WHERE portal_code=%s", (code,)).fetchone()
            pid_ = row[0] if row else ensure_portal(conn, code, code.title(), active=False)
        if portal_code:
            row = conn.execute("SELECT portal_id FROM mis.reporting_portals WHERE portal_code=%s", (portal_code,)).fetchone()
            if row:
                pid_ = row[0]
            elif create_portal:
                pid_ = ensure_portal(conn, portal_code, display_name or portal_code)
            else:
                raise ValueError("unknown portal; pass create_portal=true to register a new one")
        if pid_ is None:
            raise ValueError("portal is not known for this event; pass 'portal' (and create_portal for a new one)")
        pcode = conn.execute("SELECT portal_code FROM mis.reporting_portals WHERE portal_id=%s", (pid_,)).fetchone()[0]
        canonical_desc = {r[0]: r[1] for r in conn.execute("SELECT field_name, COALESCE(description, data_type) FROM mis.canonical_fields")}
        status_codes = {r[0] for r in conn.execute("SELECT status_code FROM mis.lead_statuses")}
        # reuse an identical pending draft instead of piling up duplicates
        sig_probe = None
        base_ids = conn.execute("""SELECT DISTINCT ON (profile_key) profile_id FROM mis.mapping_profiles
                                   WHERE portal_id=%s AND status IN ('active','proposed','draft')
                                   ORDER BY profile_key, (status='active') DESC, version DESC""", (pid_,)).fetchall()
        candidates = [load_spec_from_db(conn, r[0]) for r in base_ids]
        base = None
        hk = set(table.header_keys)
        scored = sorted(((_overlap(hk, {c.key for c in s.columns}), s) for s in candidates), key=lambda t: -t[0])
        if scored and scored[0][0] >= 0.3:
            base = scored[0][1]
        if base and base.status in ("proposed", "draft") and check_compat(base, table.header_keys).level in ("exact", "compatible"):
            _link_event(conn, settings, ev, pid_, base)             # this file waits for the same pending decision
            return _as_proposal(base, True, False, [])
    spec, used_llm, unknown_statuses = propose_spec(
        table, portal_code=pcode, filename=ev[1], base=base if base and base.status == "active" else None, aliases=aliases,
        canonical_desc=canonical_desc, status_codes=status_codes, llm=llm, profile_key=profile_key)
    if ai:
        verdict = "looks like a lead report" if ai["looks_like_lead_report"] else "does NOT look like a lead report"
        spec = replace(spec, notes=f"AI read: {verdict}; likely portal '{ai['portal_code']}' (confidence {ai['confidence']}). {ai['reason']}")
        if not ai["looks_like_lead_report"] and ai["confidence"] == "high":
            spec = replace(spec, open_questions=list(spec.open_questions) + [{
                "header": None, "blocking": True, "source": "ai",
                "question": "the AI is confident this is not a loan-lead report; reject it unless you know otherwise"}])
    with db.connection() as conn:
        errs = [e for e in validate_spec(spec, canonical_fields=set(canonical_desc), status_codes=status_codes)
                if not e.startswith("no column maps to external_lead_id")]      # already an open question
        if errs:
            raise ValueError("proposal failed validation: " + "; ".join(errs[:5]))
        pid = insert_spec(conn, spec, status="proposed", proposed_by="ai" if used_llm else "human",
                          derived_from=base.profile_id if base else None)
        saved = load_spec_from_db(conn, pid)
        _link_event(conn, settings, ev, pid_, saved)
    return _as_proposal(saved, False, used_llm, unknown_statuses)


def _link_event(conn, settings: Settings, ev, portal_id: int, spec: ProfileSpec) -> None:
    """Point the waiting file at the proposal so it is requeued and loaded when the proposal is approved."""
    info = parse_with_pattern(ev[1], spec.filename_regex, spec.report_date_format, tz=settings.tz,
                              uploaded_at=ev[4], range_end_future_policy=settings.range_end_future_policy)
    if info.ok:
        conn.execute("""UPDATE mis.drive_file_events SET portal_id=%s, profile_id=%s, report_date=%s, period_start=%s,
                           period_end=%s, filename_ts=%s, observed_at=%s, status='needs_mapping',
                           review_reason='awaiting_mapping_approval' WHERE event_id=%s""",
                     (portal_id, spec.profile_id, info.report_date, info.period_start, info.period_end, info.filename_ts,
                      info.observed_at, ev[0]))
    else:
        conn.execute("""UPDATE mis.drive_file_events SET portal_id=%s, profile_id=%s, status='needs_review',
                           review_reason=%s WHERE event_id=%s""",
                     (portal_id, spec.profile_id, "filename_pattern_proposal_needs_check:" + ",".join(info.issues or ["no_match"]),
                      ev[0]))


def _as_proposal(spec: ProfileSpec, reused: bool, used_llm: bool, unknown_statuses: list[str]) -> Proposal:
    counts = {"columns": len(spec.columns),
              "canonical": sum(c.role == "canonical" for c in spec.columns),
              "extra": sum(c.role == "extra" for c in spec.columns),
              "excluded": sum(c.role == "exclude" for c in spec.columns),
              "statuses_mapped": len(spec.status_map), "attribution_rules": len(spec.attribution_rules)}
    msg = [f"Mapping proposal #{spec.profile_id} for portal '{spec.portal_code}' (profile '{spec.profile_key}' v{spec.version}) "
           f"needs a decision. {counts['canonical']} canonical / {counts['extra']} extra / {counts['excluded']} excluded columns."]
    if spec.open_questions:
        msg.append("Open questions (block approval): " + "; ".join(
            f"[{q.get('header') or 'general'}] {q['question']}" for q in spec.open_questions[:10]))
    if spec.review_flags:
        msg.append("Must be accepted explicitly: " + ", ".join(sorted(spec.review_flags)))
    if unknown_statuses:
        msg.append(f"{len(unknown_statuses)} status value(s) have no mapping and will load as 'unmapped'.")
    return Proposal(spec.profile_id, spec.profile_key, spec.version, spec.portal_code, reused, used_llm, counts,
                    spec.open_questions, sorted(spec.review_flags), unknown_statuses, " ".join(msg))


def spec_from_dict(d: dict) -> ProfileSpec:
    """A person's corrected proposal (JSON). Same shape as a profile YAML plus portal/profile_key/filename_regex."""
    from .schema_registry import build_spec
    for k in ("portal", "profile_key", "filename_regex", "report_type", "import_mode", "file_format",
              "report_date_format", "columns"):
        if k not in d:
            raise ValueError(f"missing '{k}'")
    spec = build_spec(d, d)
    spec.open_questions = []
    return spec
