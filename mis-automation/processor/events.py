"""Drive file events: file identity, claims/leases, retries, review and replay.

`drive_file_events` (file identity: one row per Drive id + version) is deliberately separate from
`source_files` (content identity: unique by SHA-256). A claim is an atomic UPDATE; the lease protects against
crashed workers; every transition is conditional so two workers can never both own an event.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .drive_metadata import DriveFile
from .filename_parser import (HARD_ISSUES, ProfileRef, Resolution, portal_by_prefix, resolve, resolve_lenient)

OPEN_STATES = ("discovered", "failed", "needs_mapping", "needs_review", "claimed")
TERMINAL_OK = ("processed", "duplicate_content", "empty", "skipped")


class LostClaim(RuntimeError):
    pass


def profile_refs(conn) -> tuple[list[ProfileRef], dict[int, str]]:
    """Latest version per logical profile: the ACTIVE one if any, otherwise the newest draft/proposed.
    Retired-only profiles are ignored. Returns (refs, {profile_id: status})."""
    rows = conn.execute("""
        SELECT DISTINCT ON (p.portal_id, p.profile_key)
               p.profile_id, p.portal_id, rp.portal_code, p.profile_key, p.report_type, p.filename_regex,
               p.report_date_format, p.status
        FROM mis.mapping_profiles p JOIN mis.reporting_portals rp USING (portal_id)
        WHERE p.status IN ('active','proposed','draft') AND rp.is_active
        ORDER BY p.portal_id, p.profile_key, (p.status='active') DESC, p.version DESC""").fetchall()
    refs = [ProfileRef(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]
    return refs, {r[0]: r[7] for r in rows}


def register(conn, f: DriveFile, *, tz: ZoneInfo, range_policy: str, hints: set[str] | None = None,
             now: datetime | None = None, prefixes: dict[str, list[str]] | None = None,
             propose_unknown: bool = False) -> dict:
    """Idempotently records a Drive file version and decides its first state. Never alters an existing event."""
    ok, why = f.eligibility()
    refs, pstatus = profile_refs(conn)
    status, reason, res = "discovered", None, None
    lenient_row = None                                   # (portal_id, LenientResult|None) when the name was read leniently
    if not ok:
        status, reason = "skipped", why
    else:
        res = resolve(f.name, refs, tz=tz, uploaded_at=f.created_at, folder_portal_hints=hints,
                      range_end_future_policy=range_policy, now=now)
        lenient = None
        if res.status == "review" and prefixes and (res.reason == "no_profile_matches_filename" or (
                res.reason.startswith("filename_date_issue:") and res.info and not (set(res.info.issues) & HARD_ISSUES))):
            # the strict pattern failed, but the portal may still be recognisable from the START of the name:
            # judge the file by its content instead of rejecting it for how the name is written
            code = res.profile.portal_code if res.profile else None
            why = None
            if not code:
                code, why = portal_by_prefix(f.name, prefixes)
            if why:
                res = Resolution("review", reason=why)
            elif code:
                lenient = resolve_lenient(f.name, code, tz=tz, uploaded_at=f.created_at, now=now)
        if lenient is not None:
            row = conn.execute("SELECT portal_id FROM mis.reporting_portals WHERE portal_code=%s AND is_active", (lenient.portal_code,)).fetchone()
            if row and lenient.status == "ok":
                status, reason = "discovered", lenient.flag
                res = Resolution("ok", info=None)
                lenient_row = (row[0], lenient)
            else:
                status, reason = "needs_review", lenient.reason or "no_profile_matches_filename"
                res = Resolution("review", reason=reason)
                if row:
                    lenient_row = (row[0], None)
        elif res.status == "review" and propose_unknown and res.reason == "no_profile_matches_filename":
            # nobody recognises this file: let the pipeline open it and draft a mapping (and a review email) for it
            status, reason = "discovered", "unknown_file_name"
        elif res.status == "review":
            status, reason = "needs_review", res.reason
            if res.candidates:
                reason += ":" + ",".join(res.candidates)
        elif pstatus.get(res.profile.profile_id) != "active":
            status, reason = "needs_mapping", "profile_not_approved"
    info = res.info if res else None
    l_portal = lenient_row[0] if lenient_row else None
    l_res = lenient_row[1] if lenient_row else None
    row = conn.execute("""
        INSERT INTO mis.drive_file_events
          (drive_file_id, version_key, original_filename, mime_type, size_bytes, drive_md5, drive_created_at,
           drive_modified_at, drive_parent_ids, portal_id, profile_id, report_date, period_start, period_end,
           filename_ts, observed_at, status, review_reason)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (drive_file_id, version_key) DO NOTHING
        RETURNING event_id, status""",
        (f.file_id, f.version_key, f.name, f.mime_type, f.size_bytes, f.md5, f.created_at, f.modified_at,
         list(f.parents), res.profile.portal_id if res and res.profile else l_portal,
         res.profile.profile_id if res and res.profile else None,
         info.report_date if info else (l_res.report_date if l_res else None),
         info.period_start if info else None, info.period_end if info else None,
         info.filename_ts if info else None, info.observed_at if info else (l_res.observed_at if l_res else None),
         status, reason)).fetchone()
    if row:
        return {"event_id": row[0], "status": row[1], "is_new": True, "reason": reason, "drive_file_id": f.file_id}
    ex = conn.execute("SELECT event_id, status, review_reason FROM mis.drive_file_events "
                      "WHERE drive_file_id=%s AND version_key=%s", (f.file_id, f.version_key)).fetchone()
    return {"event_id": ex[0], "status": ex[1], "is_new": False, "reason": ex[2], "drive_file_id": f.file_id}


def claim(conn, event_id: int, *, lease_minutes: int, max_attempts: int) -> dict | None:
    """Atomically take ownership. Returns None if someone else holds a live lease or the event is not claimable.
    An event that has used all attempts becomes 'dead' (needs a human) instead of being claimed again."""
    conn.execute("""UPDATE mis.drive_file_events SET status='dead', claim_token=NULL, lease_expires_at=NULL,
                        last_error=COALESCE(last_error,'') || ' [max attempts reached]'
                    WHERE event_id=%s AND attempt_count >= %s
                      AND (status IN ('discovered','failed') OR (status='claimed' AND lease_expires_at < now()))""",
                 (event_id, max_attempts))
    row = conn.execute("""
        UPDATE mis.drive_file_events
           SET status='claimed', claim_token=gen_random_uuid(), claimed_at=now(),
               lease_expires_at = now() + make_interval(mins => %s), attempt_count = attempt_count + 1
         WHERE event_id=%s AND attempt_count < %s
           AND (status IN ('discovered','failed') OR (status='claimed' AND lease_expires_at < now()))
        RETURNING claim_token::text, attempt_count, drive_file_id, original_filename""",
        (lease_minutes, event_id, max_attempts)).fetchone()
    if not row:
        return None
    return {"event_id": event_id, "claim_token": row[0], "attempt": row[1], "drive_file_id": row[2], "filename": row[3]}


def finish(conn, event_id: int, token: str, status: str, *, source_file_id: int | None = None,
           reason: str | None = None, error: str | None = None) -> None:
    assert status in ("processed", "duplicate_content", "empty", "needs_mapping", "needs_review", "failed", "skipped")
    row = conn.execute("""
        UPDATE mis.drive_file_events
           SET status=%s, source_file_id=COALESCE(%s, source_file_id), review_reason=%s, last_error=%s,
               claim_token=NULL, lease_expires_at=NULL,
               processed_at = CASE WHEN %s IN ('processed','duplicate_content','empty') THEN now() ELSE processed_at END
         WHERE event_id=%s AND claim_token = %s::uuid AND status='claimed'
        RETURNING event_id""", (status, source_file_id, reason, error, status, event_id, token)).fetchone()
    if not row:
        raise LostClaim(f"event {event_id}: claim lost (lease expired or event taken over)")


def check_claim(conn, event_id: int, token: str) -> dict:
    row = conn.execute("""SELECT event_id, drive_file_id, original_filename, portal_id, profile_id, report_date,
                                 period_start, period_end, observed_at, drive_created_at, size_bytes, source_file_id,
                                 review_reason
                          FROM mis.drive_file_events
                          WHERE event_id=%s AND claim_token=%s::uuid AND status='claimed' AND lease_expires_at > now()""",
                       (event_id, token)).fetchone()
    if not row:
        raise LostClaim(f"event {event_id}: no live claim for this token")
    keys = ("event_id", "drive_file_id", "filename", "portal_id", "profile_id", "report_date", "period_start",
            "period_end", "observed_at", "drive_created_at", "size_bytes", "source_file_id", "review_reason")
    return dict(zip(keys, row))


def pending(conn, *, limit: int = 100, max_attempts: int = 3) -> list[dict]:
    """Events that should be processed now: discovered, failed with attempts left, or abandoned (expired lease)."""
    rows = conn.execute("""
        SELECT event_id, drive_file_id, original_filename, status, attempt_count, report_date
        FROM mis.drive_file_events
        WHERE (status IN ('discovered','failed') AND attempt_count < %s)
           OR (status='claimed' AND lease_expires_at < now() AND attempt_count < %s)
        ORDER BY report_date NULLS LAST, drive_created_at, event_id LIMIT %s""", (max_attempts, max_attempts, limit)).fetchall()
    return [dict(zip(("event_id", "drive_file_id", "filename", "status", "attempt_count", "report_date"), r)) for r in rows]


def resolve_review(conn, event_id: int, *, portal_code: str, profile_key: str, report_date: date,
                   period_start: date | None, period_end: date | None, tz: ZoneInfo, resolved_by: str) -> dict:
    """A person settles an ambiguous/unresolved file. Records who and why; the file then follows the normal path."""
    if not resolved_by.strip():
        raise ValueError("resolved_by is required")
    p = conn.execute("""SELECT p.profile_id, p.portal_id, p.status FROM mis.mapping_profiles p
                        JOIN mis.reporting_portals rp USING (portal_id)
                        WHERE rp.portal_code=%s AND p.profile_key=%s
                        ORDER BY (p.status='active') DESC, p.version DESC LIMIT 1""", (portal_code, profile_key)).fetchone()
    if not p:
        raise ValueError("unknown portal/profile")
    obs = datetime(report_date.year, report_date.month, report_date.day, tzinfo=tz)
    status = "discovered" if p[2] == "active" else "needs_mapping"
    row = conn.execute("""UPDATE mis.drive_file_events SET portal_id=%s, profile_id=%s, report_date=%s, period_start=%s,
                              period_end=%s, observed_at=%s, status=%s,
                              review_reason='manually resolved by ' || %s, attempt_count=0, last_error=NULL
                          WHERE event_id=%s AND status IN ('needs_review','needs_mapping','failed','dead')
                          RETURNING event_id, status""",
                       (p[1], p[0], report_date, period_start, period_end, obs, status, resolved_by.strip(), event_id)).fetchone()
    if not row:
        raise ValueError("event is not awaiting review")
    return {"event_id": row[0], "status": row[1]}


def replay(conn, event_ids: list[int], *, reason: str, reprocess_processed: bool = False) -> dict:
    """Put events back in the queue (after a mapping fix / approval). Processed events are replayed only when
    explicitly requested; the data merge is idempotent, so replaying an unchanged file changes nothing."""
    states = ["needs_mapping", "needs_review", "failed", "dead", "claimed"]
    if reprocess_processed:
        states += ["processed", "duplicate_content", "empty"]
    rows = conn.execute("""UPDATE mis.drive_file_events SET status='discovered', attempt_count=0, claim_token=NULL,
                               lease_expires_at=NULL, last_error=NULL, review_reason=%s
                           WHERE event_id = ANY(%s) AND status = ANY(%s) RETURNING event_id, source_file_id""",
                        ("replay: " + reason[:200], event_ids, states)).fetchall()
    sf = [r[1] for r in rows if r[1]]
    if reprocess_processed and sf:
        conn.execute("""UPDATE mis.source_files SET processing_status='ready', processed_at=NULL
                        WHERE source_file_id = ANY(%s) AND processing_status IN ('processed','empty')""", (sf,))
    return {"replayed": sorted(r[0] for r in rows)}


def requeue_needs_mapping(conn, profile_id: int) -> list[int]:
    """After a profile is activated, put the files that were waiting for it back in the queue."""
    p = conn.execute("SELECT portal_id, profile_key FROM mis.mapping_profiles WHERE profile_id=%s", (profile_id,)).fetchone()
    rows = conn.execute("""UPDATE mis.drive_file_events e SET status='discovered', attempt_count=0, last_error=NULL,
                                review_reason='requeued after profile activation'
                           FROM mis.mapping_profiles q
                           WHERE e.status='needs_mapping' AND q.profile_id = e.profile_id
                             AND q.portal_id=%s AND q.profile_key=%s RETURNING e.event_id""", (p[0], p[1])).fetchall()
    return [r[0] for r in rows]


def sweep_dead(conn, max_attempts: int) -> int:
    r = conn.execute("""UPDATE mis.drive_file_events SET status='dead', claim_token=NULL, lease_expires_at=NULL
                        WHERE (status='failed' OR (status='claimed' AND lease_expires_at < now()))
                          AND attempt_count >= %s""", (max_attempts,))
    return r.rowcount


ATTENTION_KEY = "attention_announced"


def attention(conn) -> list[dict]:
    """Events that need a person and have not been announced in their current state. Each is returned ONCE per state:
    needs_review (unknown/ambiguous file), needs_mapping waiting on an existing draft or approval, dead (retries
    exhausted). Mapping proposals created by the AI are announced by the proposing step itself and skipped here."""
    row = conn.execute("SELECT value FROM mis.service_state WHERE state_key=%s FOR UPDATE", (ATTENTION_KEY,)).fetchone()
    seen = dict((row[0] if row else {}).get("ids", {}))
    rows = conn.execute("""SELECT event_id, original_filename, status, review_reason, last_error FROM mis.drive_file_events
                           WHERE (status IN ('needs_review','needs_mapping','dead')
                                  AND NOT (status='needs_mapping' AND review_reason='awaiting_mapping_approval'))
                              OR (status='processed' AND review_reason LIKE 'filename_pattern_changed%')
                           ORDER BY event_id""").fetchall()
    fresh = [r for r in rows if seen.get(str(r[0])) != r[2]]
    if fresh:
        for r in fresh:
            seen[str(r[0])] = r[2]
        import json
        conn.execute("""INSERT INTO mis.service_state (state_key, value) VALUES (%s, %s::jsonb)
                        ON CONFLICT (state_key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                     (ATTENTION_KEY, json.dumps({"ids": seen})))
    return [{"event_id": r[0], "filename": r[1], "status": r[2], "reason": r[3] or r[4]} for r in fresh]
