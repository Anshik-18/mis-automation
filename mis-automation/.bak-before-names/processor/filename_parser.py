"""Profile-specific filename parsing.

Each report profile owns a regular expression (stored in mapping_profiles.filename_regex) that is
matched against the file name WITHOUT its extension(s). Named groups it may define:

    report_date                 the logical report date
    report_time                 HHMMSS (separators allowed) - interpreted in the business time zone
    period_start / period_end   for range reports

The date format of the date groups is the profile's report_date_format ('YYYYMMDD' | 'YYYY-MM-DD').
There is deliberately no global "find any 8 digits" fallback: when no profile pattern matches, or when
a date is invalid / ambiguous, the result is `review` and the file is NOT guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KNOWN_EXTENSIONS = (".csv.txt", ".tsv.txt", ".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".txt")
DATE_FORMATS = {"YYYYMMDD": "%Y%m%d", "YYYY-MM-DD": "%Y-%m-%d"}
MIN_PLAUSIBLE_DATE = date(2020, 1, 1)


def split_extension(name: str) -> tuple[str, str]:
    """('a_b.csv.txt') -> ('a_b', '.csv.txt'). Longest known extension wins; unknown => no extension."""
    lower = name.lower()
    for ext in KNOWN_EXTENSIONS:
        if lower.endswith(ext):
            return name[: -len(ext)], name[-len(ext):]
    return name, ""


def parse_date_token(token: str, fmt: str) -> date:
    if fmt not in DATE_FORMATS:
        raise ValueError(f"unsupported date format {fmt!r}")
    try:
        d = datetime.strptime(token, DATE_FORMATS[fmt]).date()
    except ValueError as exc:
        raise ValueError(f"invalid date {token!r}") from exc
    if len(token) != (8 if fmt == "YYYYMMDD" else 10):
        raise ValueError(f"invalid date {token!r}")
    return d


def parse_time_token(token: str) -> time:
    digits = re.sub(r"[^0-9]", "", token)
    if len(digits) != 6:
        raise ValueError(f"invalid time {token!r}")
    return time(int(digits[0:2]), int(digits[2:4]), int(digits[4:6]))     # raises ValueError if out of range


@dataclass
class FilenameInfo:
    matched: bool = False
    report_date: date | None = None
    report_time: time | None = None
    period_start: date | None = None
    period_end: date | None = None
    filename_ts: datetime | None = None       # timezone-aware, business tz
    observed_at: datetime | None = None       # freshness stamp (filename_ts or report_date 00:00)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.matched and self.report_date is not None and not self.issues


def parse_with_pattern(
    name: str,
    regex: str,
    date_format: str,
    *,
    tz: ZoneInfo,
    uploaded_at: datetime,
    range_end_future_policy: str = "review",
    now: datetime | None = None,
) -> FilenameInfo:
    """Apply ONE profile's pattern to a file name."""
    info = FilenameInfo()
    stem, _ext = split_extension(name)
    try:
        m = re.match(regex, stem)
    except re.error:
        info.issues.append("invalid_profile_regex")
        return info
    if not m:
        return info
    info.matched = True
    g = m.groupdict()
    now = (now or datetime.now(tz)).astimezone(tz)
    upload_day = uploaded_at.astimezone(tz).date()

    def _date(key: str) -> date | None:
        tok = g.get(key)
        if not tok:
            return None
        try:
            d = parse_date_token(tok, date_format)
        except ValueError:
            info.issues.append(f"invalid_{key}")
            return None
        if d < MIN_PLAUSIBLE_DATE:
            info.issues.append(f"implausible_{key}")
            return None
        return d

    rd, ps, pe = _date("report_date"), _date("period_start"), _date("period_end")
    info.period_start, info.period_end = ps, pe
    if (ps is None) != (pe is None) and (g.get("period_start") or g.get("period_end")):
        info.issues.append("incomplete_period")
    if ps and pe and pe < ps:
        info.issues.append("period_end_before_start")

    if rd is None and ps and pe and "period_end_before_start" not in info.issues:
        if ps == pe:
            rd = pe
        elif pe <= upload_day:
            rd = pe
            info.notes.append("report_date_taken_from_period_end")
        elif range_end_future_policy == "clamp":
            rd = upload_day
            info.notes.append("report_date_clamped_to_upload_day")
        else:
            info.issues.append("range_end_after_upload_day")      # ambiguous: do not guess
    if rd is None and not info.issues:
        info.issues.append("no_report_date")
    if rd is not None and rd > now.date() + timedelta(days=1):
        info.issues.append("report_date_in_future")
        rd = None
    info.report_date = rd

    if g.get("report_time"):
        try:
            info.report_time = parse_time_token(g["report_time"])
        except ValueError:
            info.issues.append("invalid_report_time")
    if rd is not None:
        if info.report_time is not None:
            info.filename_ts = datetime.combine(rd, info.report_time, tzinfo=tz)
            info.observed_at = info.filename_ts
        else:
            info.observed_at = datetime.combine(rd, time(0, 0), tzinfo=tz)
    return info


@dataclass(frozen=True)
class ProfileRef:
    """The subset of a profile needed to recognise a file by name."""
    profile_id: int
    portal_id: int
    portal_code: str
    profile_key: str
    report_type: str
    filename_regex: str
    report_date_format: str


@dataclass
class Resolution:
    status: str                         # 'ok' | 'review'
    profile: ProfileRef | None = None
    info: FilenameInfo | None = None
    reason: str | None = None
    candidates: list[str] = field(default_factory=list)


def resolve(
    name: str,
    profiles: list[ProfileRef],
    *,
    tz: ZoneInfo,
    uploaded_at: datetime,
    folder_portal_hints: set[str] | None = None,
    range_end_future_policy: str = "review",
    now: datetime | None = None,
) -> Resolution:
    """Pick the single approved profile whose filename pattern matches, or ask for review."""
    matches: list[tuple[ProfileRef, FilenameInfo]] = []
    for p in profiles:
        info = parse_with_pattern(
            name, p.filename_regex, p.report_date_format,
            tz=tz, uploaded_at=uploaded_at, range_end_future_policy=range_end_future_policy, now=now,
        )
        if info.matched:
            matches.append((p, info))
    if not matches:
        return Resolution("review", reason="no_profile_matches_filename")
    if folder_portal_hints and len({p.portal_code for p, _ in matches}) > 1:
        narrowed = [(p, i) for p, i in matches if p.portal_code in folder_portal_hints]
        if narrowed:
            matches = narrowed
    if len(matches) > 1:
        return Resolution("review", reason="ambiguous_profile",
                          candidates=sorted(f"{p.portal_code}/{p.profile_key}" for p, _ in matches))
    profile, info = matches[0]
    if not info.ok:
        return Resolution("review", profile=profile, info=info, reason="filename_date_issue:" + ",".join(info.issues))
    return Resolution("ok", profile=profile, info=info)
