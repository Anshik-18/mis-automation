"""Discovery window + cursor (downtime recovery).

window_start = min(now - lookback_days, cursor - overlap_hours)
  * normal day:       the cursor is recent, so the rolling `lookback_days` window applies (re-scans are idempotent);
  * after an outage:  the cursor is older than the lookback, so the window reaches back to it (minus the overlap).
The cursor only advances after a scan has registered every page, to the moment the scan STARTED, so files that
arrive during the scan are caught next time.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

KEY = "discovery_cursor"


def compute_window(now: datetime, cursor: datetime | None, lookback_days: int, overlap_hours: int) -> dict:
    lookback_start = now - timedelta(days=lookback_days)
    if cursor is None:
        return {"start": lookback_start, "mode": "initial_lookback"}
    cursor_start = cursor - timedelta(hours=overlap_hours)
    if cursor_start < lookback_start:
        return {"start": cursor_start, "mode": "recovery"}
    return {"start": lookback_start, "mode": "rolling_lookback"}


def get_cursor(conn) -> datetime | None:
    row = conn.execute("SELECT value->>'scan_started_at' FROM mis.service_state WHERE state_key=%s", (KEY,)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def advance_cursor(conn, scan_started_at: datetime) -> datetime:
    """Monotonic: an older value never moves the cursor backwards."""
    if scan_started_at.tzinfo is None:
        raise ValueError("scan_started_at must be timezone-aware")
    scan_started_at = scan_started_at.astimezone(timezone.utc)
    conn.execute("""INSERT INTO mis.service_state (state_key, value) VALUES (%s, %s::jsonb)
                    ON CONFLICT (state_key) DO UPDATE SET updated_at = now(),
                      value = CASE WHEN (mis.service_state.value->>'scan_started_at')::timestamptz < %s::timestamptz
                                   THEN EXCLUDED.value ELSE mis.service_state.value END""",
                 (KEY, json.dumps({"scan_started_at": scan_started_at.isoformat()}), scan_started_at.isoformat()))
    return get_cursor(conn)
