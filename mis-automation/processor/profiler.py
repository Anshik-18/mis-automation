"""Local, deterministic profiling of an unknown table. Nothing here leaves the process.

Produces (a) a REDACTED description safe to show an LLM - header names, inferred types, statistics and masked value
shapes - and (b) deterministic inferences (status mapping, attribution rule, date formats, filename regex).
Raw cell values reach the LLM in exactly one case: a small set of distinct values from a column that is
enum-like (<= 12 distinct values, each <= 40 chars), has a non-sensitive header, and contains nothing that looks like
a phone number, e-mail, PAN or Aadhaar. Everything else is masked (digits -> 9, letters -> a).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from .file_parser import ParsedTable
from .normalization import SENSITIVE_HEADER, blank, looks_sensitive, mask_shape, parse_date, parse_decimal

DATE_FORMATS = ("YYYY-MM-DD", "DD Mon YYYY", "YYYY-MM-DD HH24:MI:SS")
SAMPLE = 500                 # rows inspected per column
ENUM_MAX_DISTINCT = 12
IDENT_TAIL = re.compile(r"^(?P<prefix>.*?)(?P<code>[0-9A-Za-z]{3})(?P<tail>[0-9A-Fa-f]{12})$")

STATUS_KEYWORDS = [                       # ordered: first match wins; all deterministic and reviewable
    (re.compile(r"disburs\w*\s*[-_ ]?\s*pend|pend\w*\s*[-_ ]?\s*disburs"), "disbursement_pending"),
    (re.compile(r"auto\s*[-_ ]?\s*disburs|disbursed"), "disbursed"),
    (re.compile(r"reject|decline|denied"), "rejected"),
    (re.compile(r"withdraw"), "withdrawn"),
    (re.compile(r"\bclosed\b"), "closed"),
    (re.compile(r"hold|send\s*[-_ ]?\s*back"), "on_hold"),
    (re.compile(r"sanction|approv"), "approved"),
    (re.compile(r"partial"), "partial"),
    (re.compile(r"fresh|registr|new\b"), "registered"),
    (re.compile(r"in\s*[-_ ]?\s*process|document|review|underwrit"), "in_process"),
]


@dataclass
class ColumnProfile:
    header: str
    key: str
    non_blank: int
    rows: int
    distinct: int
    inferred_type: str                   # empty | boolean | integer | number | date:<fmt> | text
    sensitive_header: bool
    sensitive_values: bool
    masked_shapes: list[str]
    enum_values: list[str] | None        # only when safe (see module docstring)
    max_len: int = 0


@dataclass
class TableProfile:
    rows: int
    columns: list[ColumnProfile]
    ident_candidates: dict[str, dict] = field(default_factory=dict)      # header -> attribution inference


def _infer_type(vals: list[str]) -> str:
    if not vals:
        return "empty"
    low = {v.strip().lower() for v in vals}
    if low <= {"true", "false", "yes", "no", "y", "n", "0", "1"} and len(low) <= 2 and not low <= {"0", "1"}:
        return "boolean"
    for fmt in DATE_FORMATS:
        ok = 0
        for v in vals:
            try:
                parse_date(v, fmt)
                ok += 1
            except ValueError:
                break
        if ok == len(vals):
            return f"date:{fmt}"
    nums = 0
    for v in vals:
        try:
            parse_decimal(v)
            nums += 1
        except ValueError:
            break
    if nums == len(vals):
        return "integer" if all(re.fullmatch(r"\d+", v.strip()) for v in vals) else "number"
    return "text"


def profile_table(t: ParsedTable) -> TableProfile:
    cols: list[ColumnProfile] = []
    for i, (h, k) in enumerate(zip(t.headers, t.header_keys)):
        vals = [r[i].strip() for r in t.rows[:SAMPLE] if not blank(r[i])]
        distinct = Counter(vals)
        sens_h = bool(SENSITIVE_HEADER.search(h))
        sens_v = any(looks_sensitive(v) for v in vals[:200])
        shapes = [s for s, _ in Counter(mask_shape(v) for v in vals).most_common(5)]
        enum = None
        if vals and len(distinct) <= ENUM_MAX_DISTINCT and all(len(v) <= 40 for v in distinct) \
                and not sens_h and not sens_v and not _identifier_like(list(distinct)):
            enum = sorted(distinct)
        cols.append(ColumnProfile(h, k, len(vals), len(t.rows[:SAMPLE]), len(distinct), _infer_type(vals), sens_h,
                                  sens_v, shapes, enum, max((len(v) for v in vals), default=0)))
    tp = TableProfile(len(t.rows), cols)
    for i, c in enumerate(cols):
        vals = [r[i].strip() for r in t.rows[:SAMPLE] if not blank(r[i])]
        inf = infer_attribution(vals)
        if inf:
            tp.ident_candidates[c.header] = inf
    return tp


def _identifier_like(values: list[str]) -> bool:
    """Values that look like identifiers (long digit/hex runs) are never sent verbatim."""
    return any(re.fullmatch(r"[0-9A-Za-z\-_]{14,}", v) and re.search(r"\d", v) for v in values)


def infer_attribution(values: list[str]) -> dict | None:
    """How many values look like <prefix><3-char code><12 hex>? Returns coverage + the dominant prefix (case kept)."""
    if not values:
        return None
    matches, prefixes = 0, Counter()
    for v in values:
        m = IDENT_TAIL.match(v)
        if m:
            matches += 1
            prefixes[m.group("prefix")] += 1
    if not matches:
        return None
    coverage = matches / len(values)
    prefix, n = prefixes.most_common(1)[0]
    return {"coverage": round(coverage, 3), "dominant_prefix": prefix,
            "prefix_share": round(n / matches, 3), "matched": matches, "of": len(values)}


def pick_attribution_column(tp: TableProfile) -> tuple[str | None, list[str], dict | None]:
    """(header, ambiguous_headers, inference). Ambiguous when two columns cover within 10 points of each other."""
    ranked = sorted(((v["coverage"], h) for h, v in tp.ident_candidates.items() if v["coverage"] >= 0.5), reverse=True)
    if not ranked:
        return None, [], None
    top = ranked[0]
    close = [h for cov, h in ranked if top[0] - cov <= 0.10]
    if len(close) > 1:
        return None, close, None
    return top[1], [], tp.ident_candidates[top[1]]


def map_status_values(values: list[str]) -> tuple[dict[str, str], list[str]]:
    mapped, unknown = {}, []
    for v in values:
        low = v.strip().lower()
        for rx, code in STATUS_KEYWORDS:
            if rx.search(low):
                mapped[v] = code
                break
        else:
            unknown.append(v)
    return mapped, unknown


def suggest_filename_regex(name: str) -> tuple[str, list[str]]:
    """Generalise a concrete filename into a profile regex, using only the structure of the name.
    Returns (python_regex_for_stem, notes). Dates become named groups; a trailing 8-hex run becomes [0-9a-f]{8}."""
    from .filename_parser import split_extension
    stem = split_extension(name)[0]
    notes: list[str] = []
    out, i, used = [], 0, set()

    def group(g):
        if g in used:
            return None
        used.add(g)
        return g
    tokens = re.split(r"(\d{4}-\d{2}-\d{2}|\d{8}|\d{6}|[0-9a-f]{8}(?=$))", stem)
    nums = [t for t in tokens if re.fullmatch(r"\d{8}", t or "")]
    for tok in tokens:
        if not tok:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok):
            g = group("report_date")
            out.append(f"(?P<{g}>\\d{{4}}-\\d{{2}}-\\d{{2}})" if g else r"\d{4}-\d{2}-\d{2}")
        elif re.fullmatch(r"\d{8}", tok):
            if len(nums) >= 2:
                g = group("period_start") or group("period_end")
            else:
                g = group("report_date")
            out.append(f"(?P<{g}>\\d{{8}})" if g else r"\d{8}")
        elif re.fullmatch(r"\d{6}", tok):
            g = group("report_time")
            out.append(f"(?P<{g}>\\d{{6}})" if g else r"\d{6}")
        elif re.fullmatch(r"[0-9a-f]{8}", tok) and tok == stem[-8:]:
            out.append(r"[0-9a-f]{8}")
        else:
            out.append(re.escape(tok))
    rx = "^" + "".join(out) + "$"
    if not ({"report_date", "period_start"} & used):
        notes.append("no_date_in_filename")
    return rx, notes


def llm_view(tp: TableProfile, headers_needed: list[str]) -> dict:
    """The ONLY structure ever sent to an LLM. No raw row values except safe enum sets (see module docstring)."""
    cols = []
    for c in tp.columns:
        if c.header not in headers_needed:
            continue
        d = {"header": c.header, "type": c.inferred_type, "filled_ratio": round(c.non_blank / c.rows, 3) if c.rows else 0,
             "distinct": c.distinct, "max_len": c.max_len, "masked_shapes": c.masked_shapes}
        if c.sensitive_header or c.sensitive_values:
            d["looks_sensitive"] = True
            d["masked_shapes"] = []
        elif c.enum_values is not None:
            d["distinct_values"] = c.enum_values
        cols.append(d)
    return {"row_count": tp.rows, "columns": cols}
