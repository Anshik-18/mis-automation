"""End-of-day reconciliation: compares what the manifest expects with what the ledger shows.

Per expected report and business date the state is one of
  success        processed / duplicate content / empty-but-allowed
  in_progress    discovered, claimed, or failed with attempts left (automatic progress still possible)
  waiting_review filename/format needs a person         waiting_mapping  new layout awaiting approval
  failed         dead (retries exhausted) or an empty file that is not allowed
  missing        nothing arrived and the due time has passed      not_due  not yet due      not_expected  off-day
Batch status:  complete = every required report succeeded;  incomplete = past due and something required is
missing/failed/waiting;  open = nothing wrong yet (still arriving / in progress).
Messages contain report ids, counts and file names only - never customer data.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .manifest import ExpectedReport, Manifest

SUCCESS_EVENT = ("processed", "duplicate_content")
IN_PROGRESS = ("discovered", "claimed", "failed")
PRIORITY = {"success": 0, "in_progress": 1, "waiting_mapping": 2, "waiting_review": 3, "failed": 4}


def _event_state(row: dict, rep: ExpectedReport, max_attempts: int) -> str:
    st = row["status"]
    if st in SUCCESS_EVENT:
        return "success"
    if st == "empty":
        return "success" if rep.allow_empty else "failed"
    if st == "needs_mapping":
        return "waiting_mapping"
    if st == "needs_review":
        return "waiting_review"
    if st == "dead":
        return "failed"
    if st in IN_PROGRESS:
        return "failed" if st == "failed" and row["attempt_count"] >= max_attempts else "in_progress"
    return "in_progress"


def evaluate_date(conn, manifest: Manifest, d: date, now: datetime, max_attempts: int) -> dict:
    tz = ZoneInfo(manifest.timezone)
    items, retry_ids = [], []
    expected_keys = set()
    for rep in manifest.reports:
        report_date = d + timedelta(days=rep.report_date_offset_days)
        expected_keys.add((rep.portal, rep.profile_key))
        item = {"id": rep.id, "portal": rep.portal, "profile_key": rep.profile_key, "required": rep.required,
                "report_date": report_date.isoformat(), "files": 0, "rows": {}}
        if DAYS_NAME[d.weekday()] not in rep.days:
            item["state"] = "not_expected"
            items.append(item)
            continue
        rows = conn.execute("""
            SELECT e.event_id, e.status, e.attempt_count, e.original_filename, e.review_reason, e.source_file_id, e.profile_id,
                   sf.rows_inserted, sf.rows_updated, sf.rows_unchanged, sf.rows_quarantined, sf.rows_total, e.last_error, e.lease_expires_at
            FROM mis.drive_file_events e
            JOIN mis.mapping_profiles p ON p.profile_id = e.profile_id
            JOIN mis.reporting_portals rp ON rp.portal_id = p.portal_id
            LEFT JOIN mis.source_files sf ON sf.source_file_id = e.source_file_id
            WHERE rp.portal_code = %s AND p.profile_key = %s AND e.report_date = %s
            ORDER BY e.event_id""", (rep.portal, rep.profile_key, report_date)).fetchall()
        evs = [dict(zip(("event_id", "status", "attempt_count", "filename", "reason", "source_file_id", "profile_id",
                         "ins", "upd", "unch", "quar", "total", "error", "lease"), r)) for r in rows]
        item["files"] = len(evs)
        item["events"] = [{"event_id": e["event_id"], "filename": e["filename"], "status": e["status"],
                           "reason": e["reason"], "attempts": e["attempt_count"]} for e in evs]
        states = [(_event_state(e, rep, max_attempts), e) for e in evs]
        ok = [e for s, e in states if s == "success"]
        item["rows"] = {"inserted": sum(e["ins"] or 0 for e in ok), "updated": sum(e["upd"] or 0 for e in ok),
                        "unchanged": sum(e["unch"] or 0 for e in ok), "quarantined": sum(e["quar"] or 0 for e in ok)}
        if len(ok) >= rep.min_files:
            item["state"] = "success"
        elif states:
            item["state"] = max((s for s, _ in states if s != "success"), key=lambda s: PRIORITY[s], default="in_progress")
        else:
            due = datetime.combine(d, rep.expect_by, tzinfo=tz)
            item["state"] = "missing" if now >= due else "not_due"
            item["due_at"] = due.isoformat()
        for s, e in states:                                           # work the caller can (re)run right now
            if (e["status"] in ("discovered", "failed") and e["attempt_count"] < max_attempts) or \
               (e["status"] == "claimed" and e["lease"] is not None and e["lease"] < now and e["attempt_count"] < max_attempts):
                retry_ids.append(e["event_id"])
        items.append(item)
    # files that arrived for this date but belong to no expected report (informational)
    unexpected = conn.execute("""
        SELECT e.event_id, e.original_filename, e.status, e.review_reason
        FROM mis.drive_file_events e
        LEFT JOIN mis.mapping_profiles p ON p.profile_id = e.profile_id
        LEFT JOIN mis.reporting_portals rp ON rp.portal_id = p.portal_id
        WHERE (e.report_date = %s OR (e.report_date IS NULL AND (e.drive_created_at AT TIME ZONE %s)::date = %s))
          AND e.status <> 'skipped'
          AND COALESCE(rp.portal_code || '/' || p.profile_key, '') <> ALL(%s)
        ORDER BY e.event_id LIMIT 50""", (d, manifest.timezone, d, [f"{a}/{b}" for a, b in expected_keys])).fetchall()
    return {"items": items, "retry_event_ids": sorted(set(retry_ids)),
            "unexpected": [{"event_id": r[0], "filename": r[1], "status": r[2], "reason": r[3]} for r in unexpected]}


DAYS_NAME = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def batch_status(items: list[dict]) -> str:
    req = [i for i in items if i["required"] and i["state"] != "not_expected"]
    if req and all(i["state"] == "success" for i in req):
        return "complete"
    if not req:
        return "complete"
    if any(i["state"] in ("missing", "failed", "waiting_review", "waiting_mapping") for i in req):
        return "incomplete"
    return "open"


def compose_message(d: date, status: str, items: list[dict], unexpected: list[dict], tz_name: str) -> str:
    req = [i for i in items if i["required"] and i["state"] != "not_expected"]
    done = sum(1 for i in req if i["state"] == "success")
    lines = [f"MIS {d.isoformat()} ({tz_name}): {status.upper()} - {done}/{len(req)} required reports processed."]
    groups = {"missing": "Missing", "failed": "Failed (retries exhausted or rejected)", "waiting_mapping": "Waiting for mapping approval",
              "waiting_review": "Needs manual review", "in_progress": "In progress", "not_due": "Not yet due"}
    for state, label in groups.items():
        names = [i["id"] for i in req if i["state"] == state]
        if names:
            lines.append(f"{label}: " + ", ".join(names))
    opt = [i["id"] for i in items if not i["required"] and i["state"] in ("missing", "failed", "waiting_mapping", "waiting_review")]
    if opt:
        lines.append("Optional reports needing attention: " + ", ".join(opt))
    tot = {k: sum(i["rows"].get(k, 0) for i in items) for k in ("inserted", "updated", "unchanged", "quarantined")}
    if any(tot.values()):
        lines.append("Rows - inserted {inserted}, updated {updated}, unchanged {unchanged}, quarantined {quarantined}.".format(**tot))
    if unexpected:
        lines.append(f"{len(unexpected)} unexpected file(s): " + ", ".join(u["filename"] for u in unexpected[:5]))
    return "\n".join(lines)


def summary_hash(status: str, items: list[dict], unexpected: list[dict]) -> str:
    core = {"s": status, "i": [(i["id"], i["state"], i["files"], i["rows"]) for i in items],
            "u": [(u["event_id"], u["status"]) for u in unexpected]}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()[:16]


def should_notify(status: str, items: list[dict], new_hash: str, last_hash: str | None) -> bool:
    """Notify on change, and only when there is something to say: a problem, or the batch just completed."""
    if new_hash == last_hash:
        return False
    return status in ("complete", "incomplete")


def run(conn, manifest: Manifest, *, now: datetime | None = None, date_: date | None = None, lookback_days: int = 1,
        max_attempts: int = 3) -> dict:
    from . import events
    tz = ZoneInfo(manifest.timezone)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    events.sweep_dead(conn, max_attempts)
    today = date_ or now.date()
    batches, retry = [], []
    for k in range(max(1, lookback_days)):
        d = today - timedelta(days=k)
        prev = conn.execute("SELECT status, last_notified_hash FROM mis.daily_batches WHERE report_date=%s", (d,)).fetchone()
        if prev and prev[0] == "complete" and k > 0:
            continue                                            # closed batches are not re-evaluated
        ev = evaluate_date(conn, manifest, d, now, max_attempts)
        status = batch_status(ev["items"])
        h = summary_hash(status, ev["items"], ev["unexpected"])
        notify = should_notify(status, ev["items"], h, prev[1] if prev else None)
        msg = compose_message(d, status, ev["items"], ev["unexpected"], manifest.timezone)
        conn.execute("""INSERT INTO mis.daily_batches (report_date, status, summary, completed_at)
                        VALUES (%s,%s,%s::jsonb, CASE WHEN %s='complete' THEN now() END)
                        ON CONFLICT (report_date) DO UPDATE SET status=EXCLUDED.status, summary=EXCLUDED.summary,
                          completed_at = CASE WHEN EXCLUDED.status='complete' THEN COALESCE(mis.daily_batches.completed_at, now()) END""",
                     (d, status, json.dumps({"items": ev["items"], "unexpected": ev["unexpected"]}, default=str), status))
        retry += ev["retry_event_ids"]
        batches.append({"date": d.isoformat(), "status": status, "notify": notify, "summary_hash": h, "message": msg,
                        "items": ev["items"], "unexpected": ev["unexpected"], "retry_event_ids": ev["retry_event_ids"]})
    return {"batches": batches, "retry_event_ids": sorted(set(retry)), "now": now.isoformat()}


def acknowledge(conn, d: date, h: str) -> None:
    conn.execute("UPDATE mis.daily_batches SET last_notified_hash=%s, last_notified_at=now() WHERE report_date=%s", (h, d))
