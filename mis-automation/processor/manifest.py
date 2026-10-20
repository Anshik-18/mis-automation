"""Expected-report manifest: which reports must arrive (and be processed) on which business days.

The manifest is configuration, not code - there is no hard-coded "15 files". An entry names a portal + profile_key
(the same keys as mapping profiles), when the report is due, and how strict to be.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path

import yaml

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ExpectedReport:
    id: str
    portal: str
    profile_key: str
    required: bool = True
    days: tuple[str, ...] = DAYS
    expect_by: time = time(9, 0)             # business time zone; after this a missing file is "missing"
    report_date_offset_days: int = 0         # report_date = business date + offset (e.g. -1: yesterday's report)
    allow_empty: bool = False                # a header-only/empty file counts as success
    min_files: int = 1
    description: str = ""


@dataclass(frozen=True)
class Manifest:
    timezone: str
    reports: tuple[ExpectedReport, ...]


def parse_manifest(doc: dict) -> Manifest:
    if not isinstance(doc, dict) or "reports" not in doc:
        raise ManifestError("manifest must contain a 'reports' list")
    seen, out = set(), []
    for i, r in enumerate(doc["reports"] or []):
        try:
            rid = str(r["id"])
            if not re.fullmatch(r"[a-z0-9_]+", rid):
                raise ManifestError(f"reports[{i}].id must be lower_snake_case")
            if rid in seen:
                raise ManifestError(f"duplicate report id {rid!r}")
            seen.add(rid)
            days = tuple(str(d).lower()[:3] for d in r.get("days", DAYS))
            if not days or any(d not in DAYS for d in days):
                raise ManifestError(f"{rid}: days must be from {DAYS}")
            hh, mm = str(r.get("expect_by", "09:00")).split(":")
            out.append(ExpectedReport(
                id=rid, portal=str(r["portal"]), profile_key=str(r.get("profile_key", rid)),
                required=bool(r.get("required", True)), days=days, expect_by=time(int(hh), int(mm)),
                report_date_offset_days=int(r.get("report_date_offset_days", 0)),
                allow_empty=bool(r.get("allow_empty", False)), min_files=int(r.get("min_files", 1)),
                description=str(r.get("description", ""))))
        except KeyError as e:
            raise ManifestError(f"reports[{i}] is missing {e.args[0]!r}") from None
        except (ValueError, TypeError) as e:
            if isinstance(e, ManifestError):
                raise
            raise ManifestError(f"reports[{i}]: {e}") from None
    return Manifest(str(doc.get("timezone", "Asia/Kolkata")), tuple(out))


def load_manifest(path: str | Path) -> Manifest:
    p = Path(path)
    if not p.exists():
        raise ManifestError(f"manifest file not found: {p}")
    return parse_manifest(yaml.safe_load(p.read_text(encoding="utf-8")))
