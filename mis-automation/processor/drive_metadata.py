"""Normalises Google Drive v3 file metadata into a typed, timezone-aware record."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

FOLDER_MIME = "application/vnd.google-apps.folder"
SUPPORTED_EXTENSIONS = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm")   # .csv.txt etc. end in .txt


class DriveMetadataError(ValueError):
    pass


def parse_rfc3339(value: str) -> datetime:
    """Parse Drive timestamps ('2026-10-19T14:31:07.123Z', '+05:30' offsets). Always returns an aware datetime."""
    if not isinstance(value, str) or not value:
        raise DriveMetadataError("missing timestamp")
    v = value.strip()
    if v.endswith(("Z", "z")):
        v = v[:-1] + "+00:00"
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)           # trim sub-microsecond digits
    try:
        dt = datetime.fromisoformat(v)
    except ValueError as exc:
        raise DriveMetadataError(f"bad timestamp {value!r}") from exc
    if dt.tzinfo is None:
        raise DriveMetadataError(f"timestamp without offset {value!r}")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class DriveFile:
    file_id: str
    name: str
    mime_type: str | None
    created_at: datetime            # UTC, aware
    modified_at: datetime | None
    size_bytes: int | None
    md5: str | None
    parents: tuple[str, ...]

    @property
    def version_key(self) -> str:
        """Identifies one version of a Drive file: content hash if Drive has one, else modifiedTime."""
        if self.md5:
            return "md5:" + self.md5.lower()
        return "mod:" + (self.modified_at or self.created_at).isoformat()

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    def eligibility(self) -> tuple[bool, str | None]:
        if self.is_folder:
            return False, "folder"
        if (self.mime_type or "").startswith("application/vnd.google-apps."):
            return False, "google_native_document_not_exported"
        if not self.name.lower().endswith(SUPPORTED_EXTENSIONS):
            return False, "unsupported_extension"
        if self.size_bytes is not None and self.size_bytes == 0:
            return True, None        # empty files are registered and reported, not hidden
        return True, None


def from_api(d: dict) -> DriveFile:
    """Build a DriveFile from a Drive v3 `files` resource (or the subset n8n forwards)."""
    try:
        file_id = str(d["id"])
        name = str(d["name"])
    except KeyError as exc:
        raise DriveMetadataError(f"missing {exc.args[0]}") from exc
    created = d.get("createdTime") or d.get("created_time")
    if not created:
        raise DriveMetadataError("missing createdTime")
    size = d.get("size")
    return DriveFile(
        file_id=file_id,
        name=name,
        mime_type=d.get("mimeType"),
        created_at=parse_rfc3339(created),
        modified_at=parse_rfc3339(d["modifiedTime"]) if d.get("modifiedTime") else None,
        size_bytes=int(size) if size not in (None, "") else None,
        md5=d.get("md5Checksum") or None,
        parents=tuple(d.get("parents") or ()),
    )
