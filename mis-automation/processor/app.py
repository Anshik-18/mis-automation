"""HTTP service (Starlette). Every /v1 route requires the shared secret; /health reveals nothing but liveness."""
from __future__ import annotations

import hmac
import logging
from datetime import date, datetime, timezone

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import discovery, events, ingest, manifest as manifest_mod, migrate, mis_api, partners, proposer, reconcile, seed
from .config import Settings
from .database import Database
from .drive_metadata import DriveMetadataError, from_api
from .guards import required_acceptances, semantic_guards
from .llm import make_llm
from .schema_registry import (ProfileSpecError, approve_and_activate, insert_spec, load_spec_from_db, validate_spec)

log = logging.getLogger("mis.processor")
MAX_BATCH = 500


def _json(data, status=200):
    return JSONResponse(data, status_code=status, media_type="application/json")


def _err(code: str, message: str, status: int):
    return _json({"error": code, "message": message}, status)


def _iso(v):
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def _now():
    return datetime.now(timezone.utc)


def create_app(settings: Settings, db: Database | None = None, llm=None) -> Starlette:
    llm = llm if llm is not None else make_llm(settings)
    db = db or Database(settings.database_url, min_size=settings.pool_min_size, max_size=settings.pool_max_size)
    key = settings.api_key.encode()

    def guarded(fn):
        async def wrapper(request: Request):
            if not key:
                return _err("not_configured", "PROCESSOR_API_KEY is not set", 503)
            supplied = request.headers.get("x-api-key") or ""
            auth = request.headers.get("authorization", "")
            if not supplied and auth.lower().startswith("bearer "):
                supplied = auth[7:]
            if not hmac.compare_digest(supplied.encode(), key):
                return _err("unauthorized", "missing or invalid API key", 401)
            try:
                return await fn(request)
            except events.LostClaim as e:
                return _err("claim_lost", str(e), 409)
            except (ValueError, KeyError, ProfileSpecError, DriveMetadataError, manifest_mod.ManifestError) as e:
                return _err("bad_request", str(e)[:300], 400)
            except Exception as e:                              # never leak values; log the class only
                log.error("unhandled %s on %s", type(e).__name__, request.url.path)
                return _err("internal", type(e).__name__, 500)
        return wrapper

    def need_ledger(conn):
        st = migrate.schema_status(conn)
        if not (st["core_schema"] and st["ingestion_ledger"]):
            raise RuntimeError("schema_gap: migration 002 (ingestion ledger) is not applied")

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise ValueError("body must be JSON")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    # ------------------------------------------------------------------ routes
    async def health(request: Request):
        def ping():
            with db.connection() as c:
                c.execute("SELECT 1")
        try:
            await run_in_threadpool(ping)
        except Exception:
            return _json({"status": "degraded"}, 503)
        return _json({"status": "ok"})

    @guarded
    async def status(request: Request):
        def work():
            with db.connection() as c:
                st = migrate.schema_status(c)
                if st["ingestion_ledger"]:
                    st["events"] = dict(c.execute("SELECT status, count(*) FROM mis.drive_file_events GROUP BY 1").fetchall())
                st["profiles"] = dict(c.execute("SELECT status, count(*) FROM mis.mapping_profiles GROUP BY 1").fetchall())
                return st
        return _json(await run_in_threadpool(work))

    @guarded
    async def discovery_window(request: Request):
        def work():
            with db.connection() as c:
                need_ledger(c)
                cur = discovery.get_cursor(c)
            w = discovery.compute_window(_now(), cur, settings.discovery_lookback_days, settings.discovery_overlap_hours)
            return {"created_after": w["start"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": w["mode"],
                    "scan_started_at": _now().isoformat(), "cursor": _iso(cur),
                    "lookback_days": settings.discovery_lookback_days, "overlap_hours": settings.discovery_overlap_hours}
        return _json(await run_in_threadpool(work))

    @guarded
    async def discovery_register(request: Request):
        b = await body(request)
        files = b.get("files")
        if not isinstance(files, list) or len(files) > MAX_BATCH:
            raise ValueError(f"'files' must be a list of at most {MAX_BATCH} Drive file objects")
        hints = set(b.get("folder_portal_hints") or [])

        def work():
            out, summary = [], {}
            with db.connection() as c:
                need_ledger(c)
                for raw in files:
                    try:
                        f = from_api(raw)
                    except DriveMetadataError as e:
                        out.append({"drive_file_id": (raw or {}).get("id"), "status": "invalid_metadata", "reason": str(e)[:120]})
                        summary["invalid_metadata"] = summary.get("invalid_metadata", 0) + 1
                        continue
                    if f.is_folder:
                        continue
                    r = events.register(c, f, tz=settings.tz, range_policy=settings.range_end_future_policy, hints=hints or None)
                    r["filename"] = f.name
                    out.append(r)
                    summary[r["status"]] = summary.get(r["status"], 0) + 1
            return {"results": out, "summary": summary}
        return _json(await run_in_threadpool(work))

    @guarded
    async def discovery_complete(request: Request):
        b = await body(request)
        started = datetime.fromisoformat(str(b.get("scan_started_at")))

        def work():
            with db.connection() as c:
                need_ledger(c)
                return discovery.advance_cursor(c, started)
        cur = await run_in_threadpool(work)
        return _json({"cursor": _iso(cur)})

    @guarded
    async def events_pending(request: Request):
        limit = min(int(request.query_params.get("limit", "100")), 500)

        def work():
            with db.connection() as c:
                need_ledger(c)
                events.sweep_dead(c, settings.max_attempts)
                return events.pending(c, limit=limit, max_attempts=settings.max_attempts)
        rows = await run_in_threadpool(work)
        for r in rows:
            r["report_date"] = _iso(r["report_date"])
        return _json({"events": rows, "count": len(rows)})

    @guarded
    async def events_attention(request: Request):
        def work():
            with db.connection() as c:
                need_ledger(c)
                return events.attention(c)
        items = await run_in_threadpool(work)
        lines = [f"- {i['filename']}: {i['status']}" + (f" ({str(i['reason'])[:120]})" if i["reason"] else "") for i in items]
        text = (f"[MIS] {len(items)} file(s) need attention:\n" + "\n".join(lines)) if items else ""
        return _json({"events": items, "count": len(items), "text": text})

    @guarded
    async def event_claim(request: Request):
        eid = int(request.path_params["event_id"])

        def work():
            with db.connection() as c:
                return events.claim(c, eid, lease_minutes=settings.lease_minutes, max_attempts=settings.max_attempts)
        cl = await run_in_threadpool(work)
        if not cl:
            return _err("not_claimable", "event is already claimed, finished, or out of attempts", 409)
        return _json(cl)

    @guarded
    async def event_process(request: Request):
        eid = int(request.path_params["event_id"])
        limit = settings.max_upload_mb * 1024 * 1024
        cl = request.headers.get("content-length")
        if cl and int(cl) > limit + 65536:
            return _err("too_large", "upload exceeds MIS_MAX_UPLOAD_MB", 413)
        form = await request.form(max_part_size=limit)
        token, up = form.get("claim_token"), form.get("file")
        if not token or up is None or not hasattr(up, "read"):
            raise ValueError("multipart fields 'claim_token' and 'file' are required")
        data = await up.read()
        if len(data) > limit:
            return _err("too_large", "upload exceeds MIS_MAX_UPLOAD_MB", 413)

        def work():
            return ingest.process_event(db, settings, eid, str(token), data)
        out = await run_in_threadpool(work)
        return _json(out)

    @guarded
    async def event_fail(request: Request):
        """n8n could not obtain the file (Drive/network error): record a transient failure, keep the attempt count."""
        eid = int(request.path_params["event_id"])
        b = await body(request)

        def work():
            with db.connection() as c:
                events.check_claim(c, eid, str(b.get("claim_token")))
                events.finish(c, eid, str(b["claim_token"]), "failed", error=("download: " + str(b.get("error", "")))[:200])
                c.execute("UPDATE mis.drive_file_events SET status='dead' WHERE event_id=%s AND attempt_count >= %s",
                          (eid, settings.max_attempts))
                return c.execute("SELECT status, attempt_count FROM mis.drive_file_events WHERE event_id=%s", (eid,)).fetchone()
        st = await run_in_threadpool(work)
        return _json({"event_id": eid, "status": st[0], "attempts": st[1]})

    @guarded
    async def event_resolve(request: Request):
        eid = int(request.path_params["event_id"])
        b = await body(request)

        def work():
            with db.connection() as c:
                return events.resolve_review(
                    c, eid, portal_code=b["portal"], profile_key=b["profile_key"],
                    report_date=date.fromisoformat(b["report_date"]),
                    period_start=date.fromisoformat(b["period_start"]) if b.get("period_start") else None,
                    period_end=date.fromisoformat(b["period_end"]) if b.get("period_end") else None,
                    tz=settings.tz, resolved_by=b["resolved_by"])
        return _json(await run_in_threadpool(work))

    @guarded
    async def events_replay(request: Request):
        b = await body(request)
        ids = [int(x) for x in b.get("event_ids", [])]
        if not ids or not b.get("reason"):
            raise ValueError("event_ids and reason are required")

        def work():
            with db.connection() as c:
                return events.replay(c, ids, reason=str(b["reason"]), reprocess_processed=bool(b.get("reprocess_processed")))
        return _json(await run_in_threadpool(work))

    @guarded
    async def mapping_approve(request: Request):
        pid = int(request.path_params["profile_id"])
        b = await body(request)

        def work():
            with db.connection() as c:
                approve_and_activate(c, pid, str(b.get("approved_by", "")), list(b.get("accept_flags") or []))
                return {"profile_id": pid, "status": "active", "requeued_events": events.requeue_needs_mapping(c, pid)}
        return _json(await run_in_threadpool(work))

    @guarded
    async def mapping_get(request: Request):
        pid = int(request.path_params["profile_id"])

        def work():
            with db.connection() as c:
                sp = load_spec_from_db(c, pid)
            return {"profile_id": pid, "portal": sp.portal_code, "profile_key": sp.profile_key, "version": sp.version,
                    "status": sp.status, "proposed_by": sp.proposed_by, "report_type": sp.report_type,
                    "import_mode": sp.import_mode, "identity_namespace": sp.identity_namespace,
                    "on_unknown_lead": sp.on_unknown_lead, "filename_regex": sp.filename_regex,
                    "columns": [{"header": c.header, "role": c.role, "field": c.canonical_field, "extra_key": c.extra_key,
                                 "transform": c.transform, "date_format": c.date_format,
                                 "only_if_disbursed": c.only_if_disbursed, "reason": c.exclusion_reason} for c in sp.columns],
                    "status_map": sp.status_map,
                    "attribution_rules": [{"source_field": r.source_field, "strip_prefixes": list(r.strip_prefixes),
                                           "organic_values": list(r.organic_values)} for r in sp.attribution_rules],
                    "open_questions": sp.open_questions, "must_accept": sorted(required_acceptances(sp)),
                    "findings": [{"level": f.level, "code": f.code, "header": f.header} for f in semantic_guards(sp)]}
        return _json(await run_in_threadpool(work))

    @guarded
    async def mapping_drafts(request: Request):
        def work():
            with db.connection() as c:
                rows = c.execute("""SELECT p.profile_id, rp.portal_code, p.profile_key, p.version, p.status, p.proposed_by
                                    FROM mis.mapping_profiles p JOIN mis.reporting_portals rp USING (portal_id)
                                    WHERE p.status IN ('draft','proposed') ORDER BY p.profile_id""").fetchall()
            return [dict(zip(("profile_id", "portal", "profile_key", "version", "status", "proposed_by"), r)) for r in rows]
        return _json({"drafts": await run_in_threadpool(work)})

    @guarded
    async def mapping_propose(request: Request):
        ct = request.headers.get("content-type", "")
        if ct.startswith("multipart/"):
            form = await request.form(max_part_size=settings.max_upload_mb * 1024 * 1024)
            f = form.get("file")
            data = await f.read()
            fields = {k: v for k, v in form.items() if k != "file"}
        else:
            raise ValueError("send multipart/form-data with fields event_id, [portal], [create_portal] and file")
        eid = int(fields.get("event_id"))

        def work():
            return proposer.propose(db, settings, llm, eid, data, portal_code=fields.get("portal") or None,
                                    create_portal=str(fields.get("create_portal", "")).lower() in ("1", "true", "yes"),
                                    display_name=fields.get("display_name") or None,
                                    profile_key=fields.get("profile_key") or None)
        p = await run_in_threadpool(work)
        return _json(p.__dict__)

    @guarded
    async def mapping_submit(request: Request):
        b = await body(request)

        def work():
            spec = proposer.spec_from_dict(b["spec"])
            with db.connection() as c:
                fields = {r[0] for r in c.execute("SELECT field_name FROM mis.canonical_fields")}
                statuses = {r[0] for r in c.execute("SELECT status_code FROM mis.lead_statuses")}
                errs = validate_spec(spec, canonical_fields=fields, status_codes=statuses)
                hard = [f for f in semantic_guards(spec) if f.level == "block"]
                if errs or hard:
                    raise ValueError("; ".join(errs[:5] + [f"{f.code}({f.header})" for f in hard]))
                pid = insert_spec(c, spec, status="proposed", proposed_by="human", derived_from=b.get("derived_from"))
                sp = load_spec_from_db(c, pid)
                return {"profile_id": pid, "version": sp.version, "must_accept": sorted(required_acceptances(sp))}
        return _json(await run_in_threadpool(work))

    @guarded
    async def reconcile_run(request: Request):
        b = await body(request) if request.headers.get("content-length", "0") not in ("0", "") else {}
        mf = manifest_mod.load_manifest(settings.manifest_path)

        def work():
            with db.connection() as c:
                need_ledger(c)
                return reconcile.run(c, mf, date_=date.fromisoformat(b["date"]) if b.get("date") else None,
                                     lookback_days=int(b.get("lookback_days", 2)), max_attempts=settings.max_attempts)
        return _json(await run_in_threadpool(work))

    @guarded
    async def reconcile_ack(request: Request):
        b = await body(request)

        def work():
            with db.connection() as c:
                reconcile.acknowledge(c, date.fromisoformat(b["date"]), str(b["summary_hash"]))
            return {"ok": True}
        return _json(await run_in_threadpool(work))

    @guarded
    async def partners_register(request: Request):
        b = await body(request)

        def work():
            with db.connection() as c:
                return partners.register_partner(c, b.get("code", ""), b.get("name", ""), b.get("notes"))
        return _json(await run_in_threadpool(work))

    @guarded
    async def partners_unregistered(request: Request):
        def work():
            with db.connection() as c:
                return partners.unregistered_codes(c)
        return _json({"codes": await run_in_threadpool(work)})

    routes = [
        Route("/health", health),
        Route("/v1/status", status),
        Route("/v1/discovery/window", discovery_window),
        Route("/v1/discovery/register", discovery_register, methods=["POST"]),
        Route("/v1/discovery/complete", discovery_complete, methods=["POST"]),
        Route("/v1/events/pending", events_pending),
        Route("/v1/events/replay", events_replay, methods=["POST"]),
        Route("/v1/events/attention", events_attention, methods=["POST"]),
        Route("/v1/events/{event_id:int}/claim", event_claim, methods=["POST"]),
        Route("/v1/events/{event_id:int}/process", event_process, methods=["POST"]),
        Route("/v1/events/{event_id:int}/fail", event_fail, methods=["POST"]),
        Route("/v1/events/{event_id:int}/resolve", event_resolve, methods=["POST"]),
        Route("/v1/mapping/drafts", mapping_drafts),
        Route("/v1/mapping/propose", mapping_propose, methods=["POST"]),
        Route("/v1/mapping/submit", mapping_submit, methods=["POST"]),
        Route("/v1/mapping/{profile_id:int}", mapping_get),
        Route("/v1/mapping/{profile_id:int}/approve", mapping_approve, methods=["POST"]),
        Route("/v1/reconcile/run", reconcile_run, methods=["POST"]),
        Route("/v1/reconcile/ack", reconcile_ack, methods=["POST"]),
        Route("/v1/partners", partners_register, methods=["POST"]),
        Route("/v1/partners/unregistered", partners_unregistered),
    ]
    routes += mis_api.build_routes(guarded, db, settings)
    app = Starlette(routes=routes)
    app.state.db, app.state.settings, app.state.guarded, app.state.body = db, settings, guarded, body
    app.state.need_ledger = need_ledger
    return app
