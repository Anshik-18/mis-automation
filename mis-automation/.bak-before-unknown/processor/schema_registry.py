"""Mapping-profile model: YAML seed -> ProfileSpec, header compatibility, DB load/save, lifecycle.

A profile is DATA (rows in mapping_profile_*). Nothing here executes profile content as code; transforms come from
a closed allow-list enforced both here and by the database CHECKs.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .attribution import AttributionRule, DEFAULT_IDENTIFIER_REGEX
from .normalization import header_signature, normalize_header
from .validation import ValidationRule

TRANSFORMS = {"text", "upper", "number", "date", "yes_no_boolean", "redact_digit_runs"}
DATE_FORMATS = {"YYYY-MM-DD", "DD Mon YYYY", "YYYY-MM-DD HH24:MI:SS"}
ROLES = {"canonical", "extra", "exclude"}
SENSITIVITIES = {"none", "contact", "personal", "identity_document", "financial", "credential", "free_text"}
RULE_TYPES = {"required", "regex", "min_value", "max_value", "allowed_values", "not_after_report_date"}


class ProfileSpecError(ValueError):
    pass


@dataclass(frozen=True)
class ColumnSpec:
    header: str
    role: str
    canonical_field: str | None = None
    extra_key: str | None = None
    transform: str = "text"
    date_format: str | None = None
    only_if_disbursed: bool = False
    sensitivity: str = "none"
    exclusion_reason: str | None = None
    alias_priority: int = 1

    @property
    def key(self) -> str:
        return normalize_header(self.header)


@dataclass
class ProfileSpec:
    portal_code: str
    profile_key: str
    report_type: str
    import_mode: str
    file_format: str
    header_row: int
    filename_regex: str
    report_date_format: str
    columns: list[ColumnSpec]
    status_map: dict[str, str]
    attribution_rules: list[AttributionRule]
    validation_rules: list[ValidationRule]
    identity_namespace: str = "lead_id"
    on_unknown_lead: str = "create"
    implied_status_code: str | None = None
    footer_row_regex: str | None = None
    sheet_name: str | None = None
    layout_family: str | None = None
    notes: str | None = None
    review_flags: list[str] = field(default_factory=list)
    open_questions: list[dict] = field(default_factory=list)
    # populated when loaded from the DB
    profile_id: int | None = None
    version: int | None = None
    status: str | None = None
    proposed_by: str = "human"
    portal_id: int | None = None

    @property
    def signature(self) -> str:
        return header_signature([c.header for c in self.columns])

    def column_by_key(self) -> dict[str, ColumnSpec]:
        return {c.key: c for c in self.columns}

    def implies_disbursement_statuses(self, implies: set[str]) -> set[str]:
        return implies


# ---------------------------------------------------------------- validation of a spec ----------
def pack_notes(spec: "ProfileSpec") -> str | None:
    if not (spec.notes or spec.review_flags or spec.open_questions):
        return None
    return json.dumps({"note": spec.notes, "flags": sorted(set(spec.review_flags)), "open_questions": spec.open_questions})


def unpack_notes(text: str | None) -> tuple[str | None, list[str], list[dict]]:
    if not text:
        return None, [], []
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            return d.get("note"), list(d.get("flags", [])), list(d.get("open_questions", []))
    except ValueError:
        pass
    return text, [], []


def validate_spec(spec: ProfileSpec, *, canonical_fields: set[str] | None = None,
                  status_codes: set[str] | None = None) -> list[str]:
    """Returns a list of problems (empty = structurally valid). Does not judge business semantics -
    see proposer.semantic_guards for the Loan Amt / Approved Amt / Net Disbursed rules."""
    errs: list[str] = []
    keys = [c.key for c in spec.columns]
    if len(keys) != len(set(keys)):
        errs.append("duplicate column headers in profile")
    for c in spec.columns:
        if c.role not in ROLES:
            errs.append(f"{c.header}: invalid role {c.role!r}")
        if c.transform not in TRANSFORMS:
            errs.append(f"{c.header}: transform {c.transform!r} not in allow-list")
        if (c.transform == "date") != (c.date_format is not None):
            errs.append(f"{c.header}: date_format is required exactly when transform is 'date'")
        if c.date_format and c.date_format not in DATE_FORMATS:
            errs.append(f"{c.header}: unsupported date_format {c.date_format!r}")
        if c.sensitivity not in SENSITIVITIES:
            errs.append(f"{c.header}: invalid sensitivity")
        if c.role == "canonical":
            if not c.canonical_field:
                errs.append(f"{c.header}: canonical column needs a field")
            elif canonical_fields is not None and c.canonical_field not in canonical_fields:
                errs.append(f"{c.header}: unknown canonical field {c.canonical_field!r}")
        elif c.canonical_field:
            errs.append(f"{c.header}: only canonical columns may name a field")
        if c.role == "extra" and not (c.extra_key and re.fullmatch(r"[a-z0-9_]+", c.extra_key)):
            errs.append(f"{c.header}: extra column needs a snake_case extra_key")
        if c.role == "exclude" and not c.exclusion_reason:
            errs.append(f"{c.header}: excluded column needs a reason")
        if c.only_if_disbursed and not (c.role == "canonical" and c.extra_key):
            errs.append(f"{c.header}: only_if_disbursed needs a canonical column with a parked extra_key")
    fields = [c.canonical_field for c in spec.columns if c.role == "canonical" and c.alias_priority == 1]
    dup = sorted({f for f in fields if f and fields.count(f) > 1})
    if dup:
        errs.append(f"several columns map to the same field at the same priority: {dup}")
    if not any(c.canonical_field == "external_lead_id" for c in spec.columns):
        errs.append("no column maps to external_lead_id (identity)")
    if spec.file_format not in ("csv", "xlsx"):
        errs.append("file_format must be csv or xlsx")
    if spec.report_date_format not in ("YYYYMMDD", "YYYY-MM-DD"):
        errs.append("report_date_format must be YYYYMMDD or YYYY-MM-DD")
    if spec.on_unknown_lead not in ("create", "quarantine", "skip"):
        errs.append("on_unknown_lead invalid")
    try:
        re.compile(spec.filename_regex)
    except re.error as e:
        errs.append(f"filename_regex does not compile: {e}")
    if spec.footer_row_regex:
        try:
            re.compile(spec.footer_row_regex)
        except re.error as e:
            errs.append(f"footer_row_regex does not compile: {e}")
    for r in spec.attribution_rules:
        try:
            rx = re.compile(r.identifier_regex)
            if rx.groups < 1:
                errs.append("attribution identifier_regex needs a capture group")
        except re.error as e:
            errs.append(f"attribution regex invalid: {e}")
        if r.source_field not in ("utm_source", "utm_medium", "utm_campaign", "utm_term"):
            errs.append(f"attribution source_field {r.source_field!r} not allowed")
    for v in spec.validation_rules:
        if v.rule_type not in RULE_TYPES:
            errs.append(f"validation rule type {v.rule_type!r} not allowed")
    if status_codes is not None:
        bad = sorted({v for v in spec.status_map.values() if v not in status_codes})
        if bad:
            errs.append(f"status_map targets unknown status codes: {bad}")
        if spec.implied_status_code and spec.implied_status_code not in status_codes:
            errs.append("implied_status_code unknown")
    return errs


# ---------------------------------------------------------------- YAML seeds ----------------------
def _col(d: dict) -> ColumnSpec:
    return ColumnSpec(
        header=d["header"], role=d["role"], canonical_field=d.get("field"),
        extra_key=d.get("parked_key") or d.get("extra_key"),
        transform=d.get("transform", "text"), date_format=d.get("date_format"),
        only_if_disbursed=bool(d.get("only_if_disbursed", False)),
        sensitivity=d.get("sensitivity", "none"), exclusion_reason=d.get("exclusion_reason"),
        alias_priority=int(d.get("alias_priority", 1)))


def load_layout(layouts_dir: Path, name: str, _seen: tuple[str, ...] = ()) -> dict:
    if name in _seen:
        raise ProfileSpecError(f"layout extends cycle at {name}")
    raw = yaml.safe_load((layouts_dir / f"{name}.yaml").read_text(encoding="utf-8"))
    if "extends" in raw:
        base = load_layout(layouts_dir, raw["extends"], _seen + (name,))
        merged = copy.deepcopy(base)
        for k, v in raw.items():
            if k in ("extends", "extra_columns"):
                continue
            merged[k] = v
        merged["columns"] = list(base["columns"]) + list(raw.get("extra_columns", []))
        return merged
    return raw


def build_spec(entry: dict, layout: dict, portal_code: str | None = None) -> ProfileSpec:
    rules = [AttributionRule(
        priority=int(r.get("priority", i + 1)), source_field=r["source_field"],
        strip_prefixes=tuple(r.get("strip_prefixes", [])),
        identifier_regex=r.get("identifier_regex", DEFAULT_IDENTIFIER_REGEX),
        organic_values=tuple(r.get("organic_values", []))) for i, r in enumerate(entry.get("attribution_rules", []))]
    vrules = [ValidationRule(v["field"], v["rule"], v.get("param", {}), v.get("on_fail", "quarantine_row"))
              for v in layout.get("validation_rules", [])]
    return ProfileSpec(
        portal_code=portal_code or entry["portal"], profile_key=entry["profile_key"],
        report_type=layout["report_type"], import_mode=layout["import_mode"],
        file_format=layout["file_format"], header_row=int(layout.get("header_row", 1)),
        filename_regex=entry["filename_regex"], report_date_format=layout["report_date_format"],
        columns=[_col(c) for c in layout["columns"]],
        status_map={str(k): str(v) for k, v in (layout.get("status_map") or {}).items()},
        attribution_rules=rules, validation_rules=vrules,
        identity_namespace=layout.get("identity_namespace", "lead_id"),
        on_unknown_lead=layout.get("on_unknown_lead", "create"),
        implied_status_code=layout.get("implied_status_code"),
        footer_row_regex=layout.get("footer_row_regex"), sheet_name=layout.get("sheet_name"),
        layout_family=layout.get("layout_family"), notes=entry.get("notes"),
        review_flags=list(entry.get("review_flags", [])))


def load_seed_specs(seed_dir: Path) -> list[ProfileSpec]:
    seed_dir = Path(seed_dir)
    doc = yaml.safe_load((seed_dir / "profiles.yaml").read_text(encoding="utf-8"))
    out = []
    for entry in doc["profiles"]:
        layout = load_layout(seed_dir / "layouts", entry["layout"])
        out.append(build_spec(entry, layout))
    return out


def load_portals(seed_dir: Path) -> list[dict]:
    return yaml.safe_load((Path(seed_dir) / "portals.yaml").read_text(encoding="utf-8"))["portals"]


def load_aliases(path: Path) -> dict[str, dict]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))["aliases"]


# ---------------------------------------------------------------- compatibility -------------------
@dataclass(frozen=True)
class Compat:
    level: str                       # exact | compatible | incompatible
    missing_required: tuple[str, ...] = ()
    missing_optional: tuple[str, ...] = ()
    unknown_headers: tuple[str, ...] = ()
    reordered: bool = False


def check_compat(spec: ProfileSpec, header_keys: list[str]) -> Compat:
    """exact: same header set.  compatible: every column that carries a CANONICAL value or the identity is present,
    only excluded/extra columns are missing, and no unknown column appears.  Otherwise incompatible.
    Unknown (new) headers are never silently dropped: they make the file incompatible so a human reviews them."""
    by_key = spec.column_by_key()
    have, want = set(header_keys), set(by_key)
    missing = want - have
    unknown = have - want
    needed = {k for k, c in by_key.items() if c.role == "canonical"}
    missing_req = sorted(missing & needed)
    missing_opt = sorted(missing - needed)
    reordered = [k for k in header_keys if k in want] != [c.key for c in spec.columns if c.key in have]
    if not missing and not unknown:
        return Compat("exact", reordered=reordered)
    # a file lacking only optional canonical columns would still be deterministic, but we are strict: any canonical
    # column absent (e.g. the disbursement amount) changes meaning, so it is incompatible.
    if missing_req or unknown:
        return Compat("incompatible", tuple(missing_req), tuple(missing_opt), tuple(sorted(unknown)), reordered)
    return Compat("compatible", (), tuple(missing_opt), (), reordered)


def select_profile(candidates: list[ProfileSpec], header_keys: list[str]) -> tuple[ProfileSpec | None, Compat | None, str]:
    """Among filename-matched ACTIVE candidates pick the one whose headers fit. Returns (spec, compat, reason)."""
    best, best_c = [], []
    for s in candidates:
        c = check_compat(s, header_keys)
        if c.level in ("exact", "compatible"):
            best.append(s)
            best_c.append(c)
    if len(best) == 1:
        return best[0], best_c[0], "ok"
    if len(best) > 1:
        return None, None, "ambiguous_profile_for_headers"
    return None, None, "headers_incompatible" if candidates else "no_active_profile"


# ---------------------------------------------------------------- DB load / save ------------------
def load_spec_from_db(conn, profile_id: int) -> ProfileSpec:
    with conn.cursor() as cur:
        cur.execute("""SELECT p.profile_id, p.profile_key, p.version, p.report_type, p.import_mode, p.identity_namespace,
                              p.layout_family, p.file_format, p.sheet_name, p.header_row, p.filename_regex,
                              p.report_date_format, p.footer_row_regex, p.on_unknown_lead, p.implied_status_code,
                              p.status, p.proposed_by, p.notes, p.portal_id, rp.portal_code
                       FROM mis.mapping_profiles p JOIN mis.reporting_portals rp USING (portal_id)
                       WHERE p.profile_id = %s""", (profile_id,))
        r = cur.fetchone()
        if not r:
            raise KeyError(f"profile {profile_id} not found")
        (pid, pkey, ver, rtype, mode, ns, fam, fmt, sheet, hrow, frx, rdf, foot, onu, implied, status,
         by, notes, portal_id, portal_code) = r
        cur.execute("""SELECT source_header, role, canonical_field, extra_key, transform, date_format,
                              only_if_status_implies_disbursement, sensitivity, exclusion_reason, alias_priority
                       FROM mis.mapping_profile_columns WHERE profile_id=%s ORDER BY column_map_id""", (pid,))
        cols = [ColumnSpec(h, role, cf, ek, tr, df, bool(oid), sens, er, int(ap))
                for (h, role, cf, ek, tr, df, oid, sens, er, ap) in cur.fetchall()]
        cur.execute("SELECT source_status, status_code FROM mis.mapping_profile_status_map WHERE profile_id=%s", (pid,))
        smap = {s: c for s, c in cur.fetchall()}
        cur.execute("""SELECT priority, source_field, strip_prefixes, identifier_regex, organic_values, rule_id
                       FROM mis.mapping_profile_attribution_rules WHERE profile_id=%s ORDER BY priority""", (pid,))
        arules = [AttributionRule(int(p), sf, tuple(sp or ()), rx, tuple(ov or ()), rid)
                  for (p, sf, sp, rx, ov, rid) in cur.fetchall()]
        cur.execute("""SELECT canonical_field, rule_type, rule_param, on_fail
                       FROM mis.mapping_profile_validation_rules WHERE profile_id=%s""", (pid,))
        vrules = [ValidationRule(cf, rt, rp or {}, of) for (cf, rt, rp, of) in cur.fetchall()]
    note, flags, questions = unpack_notes(notes)
    return ProfileSpec(portal_code=portal_code, profile_key=pkey, report_type=rtype, import_mode=mode,
                       file_format=fmt, header_row=int(hrow), filename_regex=frx, report_date_format=rdf,
                       columns=cols, status_map=smap, attribution_rules=arules, validation_rules=vrules,
                       identity_namespace=ns, on_unknown_lead=onu, implied_status_code=implied,
                       footer_row_regex=foot, sheet_name=sheet, layout_family=fam, notes=note,
                       review_flags=flags, open_questions=questions, profile_id=pid, version=int(ver), status=status, proposed_by=by, portal_id=portal_id)


def load_active_specs(conn, portal_code: str | None = None) -> list[ProfileSpec]:
    with conn.cursor() as cur:
        if portal_code:
            cur.execute("""SELECT profile_id FROM mis.mapping_profiles p JOIN mis.reporting_portals rp USING (portal_id)
                           WHERE p.status='active' AND rp.portal_code=%s ORDER BY profile_id""", (portal_code,))
        else:
            cur.execute("SELECT profile_id FROM mis.mapping_profiles WHERE status='active' ORDER BY profile_id")
        ids = [r[0] for r in cur.fetchall()]
    return [load_spec_from_db(conn, i) for i in ids]


def ensure_portal(conn, code: str, display_name: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO mis.reporting_portals (portal_code, display_name) VALUES (%s, %s)
                       ON CONFLICT (portal_code) DO UPDATE SET portal_code = EXCLUDED.portal_code
                       RETURNING portal_id""", (code, display_name or code))
        return cur.fetchone()[0]


def next_version(conn, portal_id: int, profile_key: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(max(version),0)+1 FROM mis.mapping_profiles WHERE portal_id=%s AND profile_key=%s",
                    (portal_id, profile_key))
        return cur.fetchone()[0]


def insert_spec(conn, spec: ProfileSpec, *, status: str = "proposed", proposed_by: str = "human",
                derived_from: int | None = None) -> int:
    """Inserts a NEW profile version (status draft/proposed). Never updates an approved version."""
    if status not in ("draft", "proposed"):
        raise ProfileSpecError("insert_spec only creates draft/proposed profiles; use approve/activate")
    portal_id = ensure_portal(conn, spec.portal_code)
    version = next_version(conn, portal_id, spec.profile_key)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO mis.mapping_profiles
              (portal_id, profile_key, version, report_type, import_mode, identity_namespace, layout_family,
               derived_from_profile_id, file_format, sheet_name, header_row, header_signature, filename_regex,
               report_date_format, footer_row_regex, on_unknown_lead, implied_status_code, status, proposed_by, notes)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING profile_id""",
            (portal_id, spec.profile_key, version, spec.report_type, spec.import_mode, spec.identity_namespace,
             spec.layout_family, derived_from, spec.file_format, spec.sheet_name, spec.header_row, spec.signature,
             spec.filename_regex, spec.report_date_format, spec.footer_row_regex, spec.on_unknown_lead,
             spec.implied_status_code, status, proposed_by, pack_notes(spec)))
        pid = cur.fetchone()[0]
        for c in spec.columns:
            cur.execute("""INSERT INTO mis.mapping_profile_columns
                  (profile_id, source_header, role, canonical_field, extra_key, alias_priority, transform, date_format,
                   only_if_status_implies_disbursement, sensitivity, exclusion_reason)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (pid, c.header, c.role, c.canonical_field, c.extra_key, c.alias_priority, c.transform, c.date_format,
                 c.only_if_disbursed, c.sensitivity, c.exclusion_reason))
        for s, code in spec.status_map.items():
            cur.execute("INSERT INTO mis.mapping_profile_status_map (profile_id, source_status, status_code) VALUES (%s,%s,%s)"
                        " ON CONFLICT (profile_id, source_status_key) DO NOTHING", (pid, s, code))
        for r in spec.attribution_rules:
            cur.execute("""INSERT INTO mis.mapping_profile_attribution_rules
                  (profile_id, priority, source_field, strip_prefixes, identifier_regex, organic_values)
                  VALUES (%s,%s,%s,%s,%s,%s)""",
                (pid, r.priority, r.source_field, list(r.strip_prefixes), r.identifier_regex, list(r.organic_values)))
        for v in spec.validation_rules:
            cur.execute("""INSERT INTO mis.mapping_profile_validation_rules
                  (profile_id, canonical_field, rule_type, rule_param, on_fail) VALUES (%s,%s,%s,%s::jsonb,%s)""",
                (pid, v.canonical_field, v.rule_type, json.dumps(v.rule_param), v.on_fail))
    return pid


def approve_and_activate(conn, profile_id: int, approved_by: str, accept_flags: list[str] | tuple[str, ...] = ()) -> None:
    """proposed -> approved -> active, retiring the previously active version of the same logical profile.
    The caller commits. Approval is always a named human decision, and it must explicitly accept every review flag
    (for example 'net_disbursed_amount_semantics'); blocking findings and open questions can never be accepted -
    submit a corrected version instead."""
    from .guards import blockers, required_acceptances
    if not approved_by or not approved_by.strip():
        raise ProfileSpecError("approved_by is required")
    spec = load_spec_from_db(conn, profile_id)
    if spec.status not in ("draft", "proposed"):
        raise ProfileSpecError(f"profile is {spec.status}; only draft/proposed profiles can be approved")
    hard = blockers(spec)
    if hard:
        raise ProfileSpecError("blocking findings: " + "; ".join(f"{b.code}({b.header})" for b in hard))
    if spec.open_questions:
        raise ProfileSpecError(f"{len(spec.open_questions)} open question(s) must be resolved by submitting a corrected version")
    missing = sorted(required_acceptances(spec) - set(accept_flags))
    if missing:
        raise ProfileSpecError("approval must explicitly accept: " + ", ".join(missing))
    with conn.cursor() as cur:
        cur.execute("SELECT portal_id, profile_key, status FROM mis.mapping_profiles WHERE profile_id=%s FOR UPDATE",
                    (profile_id,))
        row = cur.fetchone()
        if not row:
            raise KeyError("profile not found")
        portal_id, key, status = row
        if status not in ("draft", "proposed"):
            raise ProfileSpecError(f"profile is {status}; only draft/proposed profiles can be approved")
        cur.execute("""UPDATE mis.mapping_profiles SET status='retired', retired_at=now()
                       WHERE portal_id=%s AND profile_key=%s AND status='active'""", (portal_id, key))
        cur.execute("""UPDATE mis.mapping_profiles SET status='active', approved_by=%s, approved_at=now(),
                              activated_at=now() WHERE profile_id=%s""", (approved_by.strip(), profile_id))


def reject_available(conn) -> bool:
    """True once migration 004 (the 'rejected' status) is applied."""
    return bool(conn.execute("""SELECT 1 FROM information_schema.columns WHERE table_schema='mis'
                                AND table_name='mapping_profiles' AND column_name='rejected_by'""").fetchone())


def reject_profile(conn, profile_id: int, rejected_by: str, reason: str = "") -> list[int]:
    """draft/proposed -> rejected. Files held for this proposal move to needs_review (they are NOT requeued and will not
    be picked up by another proposal until someone proposes again). Returns the held event ids. The caller commits."""
    if not rejected_by or not rejected_by.strip():
        raise ProfileSpecError("rejected_by is required")
    if not reject_available(conn):
        raise ProfileSpecError("schema_gap: migration 004 (profile rejection) is not applied; add 004 to MIS_APPROVED_MIGRATIONS")
    row = conn.execute("SELECT status FROM mis.mapping_profiles WHERE profile_id=%s FOR UPDATE", (profile_id,)).fetchone()
    if not row:
        raise KeyError("profile not found")
    if row[0] not in ("draft", "proposed"):
        raise ProfileSpecError(f"profile is {row[0]}; only draft/proposed profiles can be rejected")
    conn.execute("""UPDATE mis.mapping_profiles SET status='rejected', rejected_by=%s, rejected_at=now(), rejected_reason=%s
                    WHERE profile_id=%s""", (rejected_by.strip()[:120], (reason or "").strip()[:500] or None, profile_id))
    held = conn.execute("""UPDATE mis.drive_file_events SET status='needs_review', review_reason=%s
                           WHERE profile_id=%s AND status='needs_mapping' RETURNING event_id""",
                        (f"mapping_rejected:{profile_id}", profile_id)).fetchall()
    return [r[0] for r in held]
