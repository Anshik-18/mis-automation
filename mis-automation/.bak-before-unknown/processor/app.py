"""HTTP service (Starlette). Every /v1 route requires the shared secret; /health reveals nothing but liveness."""
from __future__ import annotations

import hmac
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from . import discovery, events, ingest, manifest as manifest_mod, migrate, mis_api, partners, proposer, reconcile, review, seed
from .config import Settings
from .database import Database
from .drive_metadata import DriveMetadataError, from_api
from .guards import required_acceptances, semantic_guards
from .llm import make_llm
from .schema_registry import (load_portals, ProfileSpecError, approve_and_activate, reject_profile, insert_spec, load_spec_from_db, validate_spec)

log = logging.getLogger("mis.processor")
MAX_BATCH = 500


def _json(data, status=200):
    return JSONResponse(data, status_code=status, media_type="application/json")


def _err(code: str, message: str, status: int):
    return _json({"error": code, "message": message}, status)


def _attention_line(i: dict) -> str:
    """One plain sentence per file. File names and reason codes only - never row values."""
    r = str(i.get("reason") or "")
    if i["status"] == "processed" and r.startswith("filename_pattern_changed"):
        what = r.split(":", 1)[-1].replace("_", " ").replace(",", "; ")
        why = f"loaded, but the file name did not match the usual pattern ({what[:120]})"
    elif r.startswith("no_profile_matches_filename"):
        why = "not recognised: the name does not start like any known portal's report - this may be a different kind of file"
    elif r.startswith("filename_multiple_dates"):
        why = "the name contains several dates, so the report date was not guessed"
    elif r.startswith("ambiguous_portal_prefix"):
        why = "the name starts like more than one portal"
    elif r.startswith("mapping_rejected"):
        why = "held: its mapping proposal was rejected; submit a corrected mapping"
    else:
        why = f"{i['status']}" + (f" ({r[:120]})" if r else "")
    return f"{i['filename']}: {why}"


def _iso(v):
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def _now():
    return datetime.now(timezone.utc)


def create_app(settings: Settings, db: Database | None = None, llm=None) -> Starlette:
    llm = llm if llm is not None else make_llm(settings)
    db = db or Database(settings.database_url, min_size=settings.pool_min_size, max_size=settings.pool_max_size)
    key = settings.api_key.encode()
    try:                                                    # portal name prefixes for recognising renamed files
        portal_prefixes = {p["code"]: [p["code"], *(p.get("filename_prefixes") or [])]
                           for p in load_portals(Path(settings.seed_dir))}
    except Exception:
        portal_prefixes = {}

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
                    r = events.register(c, f, tz=settings.tz, range_policy=settings.range_end_future_policy, hints=hints or None, prefixes=portal_prefixes or None)
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
        say = [_attention_line(i) for i in items]
        text = (f"[MIS] {len(items)} file(s) need attention:\n" + "\n".join(f"- {x}" for x in say)) if items else ""
        subject = f"[MIS] {len(items)} file(s) need attention" if items else ""
        html_ = ("<p>" + review.html.escape(f"{len(items)} file(s) need attention:") + "</p><ul>" +
                 "".join(f"<li>{review.html.escape(x)}</li>" for x in say) + "</ul>") if items else ""
        return _json({"events": items, "count": len(items), "text": text, "email_subject": subject, "email_html": html_})

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

    def review_link(profile_id: int, digest: str) -> str | None:
        if not (settings.review_secret and settings.public_base_url):
            return None
        tok = review.sign_token(settings.review_secret, profile_id, digest, settings.review_ttl_hours * 3600)
        return f"{settings.public_base_url}/review/{tok}"

    def review_bundle(conn, profile_id: int) -> dict:
        r = review.build_review(conn, profile_id)
        link = review_link(profile_id, r["digest"])
        md = review.render_markdown(r)
        subject = f"[MIS] Mapping proposal #{profile_id} for {r['portal']}: " + \
            ("REVIEW CAREFULLY - money/date/status columns affected" if r["critical_changed"] else "layout change to approve")
        text = md + ("\n\nReview and approve: " + link if link else "\n\nApprove via the API or ask your assistant to approve it.")
        return {**r, "review_markdown": md, "review_url": link, "email_subject": subject, "email_text": text,
                "email_html": review.render_html(r) + (f'<p><a href="{link}">Open the review page</a> (nothing is approved until you press Approve there)</p>' if link else "")}

    def do_approve(conn, pid: int, approved_by: str, flags: list[str], expected_digest: str | None, digest_is_prefix=False):
        spec = load_spec_from_db(conn, pid)
        if expected_digest:
            cur = review.digest_of(spec)
            ok = cur.startswith(expected_digest) if digest_is_prefix else cur == expected_digest
            if not ok:
                return None
        approve_and_activate(conn, pid, approved_by, flags)
        return {"profile_id": pid, "status": "active", "requeued_events": events.requeue_needs_mapping(conn, pid)}

    @guarded
    async def mapping_approve(request: Request):
        pid = int(request.path_params["profile_id"])
        b = await body(request)

        def work():
            with db.connection() as c:
                return do_approve(c, pid, str(b.get("approved_by", "")), list(b.get("accept_flags") or []),
                                  str(b["expected_digest"]) if b.get("expected_digest") else None)
        res = await run_in_threadpool(work)
        if res is None:
            return _err("proposal_changed", "the proposal changed since it was reviewed; fetch /review again", 409)
        return _json(res)

    def do_reject(conn, pid: int, who: str, reason: str, expected_digest: str | None, digest_is_prefix=False):
        spec = load_spec_from_db(conn, pid)
        if expected_digest:
            cur = review.digest_of(spec)
            if not (cur.startswith(expected_digest) if digest_is_prefix else cur == expected_digest):
                return None
        return {"profile_id": pid, "status": "rejected", "held_events": reject_profile(conn, pid, who, reason)}

    @guarded
    async def mapping_reject(request: Request):
        pid = int(request.path_params["profile_id"])
        b = await body(request)

        def work():
            with db.connection() as c:
                return do_reject(c, pid, str(b.get("rejected_by", "")), str(b.get("reason", "")),
                                 str(b["expected_digest"]) if b.get("expected_digest") else None)
        res = await run_in_threadpool(work)
        if res is None:
            return _err("proposal_changed", "the proposal changed since it was reviewed; fetch /review again", 409)
        return _json(res)

    @guarded
    async def mapping_review(request: Request):
        pid = int(request.path_params["profile_id"])

        def work():
            with db.connection() as c:
                return review_bundle(c, pid)
        return _json(await run_in_threadpool(work))

    # ---- email path: token-gated, no API key. GET only shows; approving needs a POST from the page's button. ----
    def page(title: str, inner: str, status: int = 200):
        doc = ("<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
               "<title>" + title + "</title><style>body{font:15px/1.5 system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem}"
               "button{font:inherit;padding:.6rem 1.2rem;background:#0a6;color:#fff;border:0;border-radius:6px;cursor:pointer}"
               "input[type=text]{font:inherit;padding:.4rem;width:16rem}code{background:#eee;padding:0 .25rem}</style></head><body>"
               + inner + "</body></html>")
        return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                            "Referrer-Policy": "no-referrer",
                            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"})

    def token_or_page(request: Request):
        try:
            pid, dg = review.verify_token(settings.review_secret, request.path_params["token"])
            return pid, dg, None
        except ValueError as e:
            msg = "This link has expired. Ask for a fresh one (re-run the proposal)." if str(e) == "expired" else "This link is not valid."
            return None, None, page("Link problem", f"<h2>{msg}</h2>", 410 if str(e) == "expired" else 403)

    async def review_page(request: Request):
        if not settings.review_secret:
            return page("Not configured", "<h2>Review links are not enabled.</h2>", 503)
        pid, dg, bad = token_or_page(request)
        if bad:
            return bad

        def work():
            with db.connection() as c:
                return review.build_review(c, pid)
        r = await run_in_threadpool(work)
        if not r["digest"].startswith(dg):
            return page("Changed", "<h2>This proposal changed after the link was sent. Ask for a fresh review.</h2>", 409)
        inner = review.render_html(r)
        base = "/review/" + review.html.escape(request.path_params["token"])      # absolute: a relative action resolves to /review/approve
        if r["can_approve"]:
            boxes = "".join(f'<p><label><input type=checkbox name=accept value="{review.html.escape(f)}"> I accept: <code>{review.html.escape(f)}</code></label></p>'
                            for f in r["must_accept"])
            inner += ('<form method=post action="' + base + '/approve"><p>Your name: <input type=text name=approved_by required maxlength=80></p>'
                      + boxes + '<p><button type=submit>Approve this mapping</button></p></form>')
        elif r["status"] in ("draft", "proposed"):
            inner += "<p><b>This proposal cannot be approved as it stands.</b> Submit a corrected mapping.</p>"
        if r["can_reject"]:
            inner += ('<hr><form method=post action="' + base + '/reject"><p>Not right? Your name: <input type=text name=rejected_by required maxlength=80></p>'
                      '<p>Reason (optional): <input type=text name=reason maxlength=300></p>'
                      '<p><button type=submit style="background:#b33">Reject this mapping</button></p></form>')
        return page("Mapping review", inner)

    async def review_reject(request: Request):
        if not settings.review_secret:
            return page("Not configured", "<h2>Review links are not enabled.</h2>", 503)
        pid, dg, bad = token_or_page(request)
        if bad:
            return bad
        form = await request.form()
        who = str(form.get("rejected_by", "")).strip()[:80]
        if not who:
            return page("Name needed", "<h2>Please enter your name.</h2>", 400)

        def work():
            with db.connection() as c:
                return do_reject(c, pid, "email-link:" + who, str(form.get("reason", "")), dg, digest_is_prefix=True)
        try:
            res = await run_in_threadpool(work)
        except (ProfileSpecError, ValueError, KeyError) as e:
            return page("Not rejected", f"<h2>Not rejected</h2><p>{review.html.escape(str(e)[:300])}</p>", 400)
        if res is None:
            return page("Changed", "<h2>This proposal changed after the link was sent. Ask for a fresh review.</h2>", 409)
        return page("Rejected", f"<h2>Rejected</h2><p>Mapping #{pid} will not be used. {len(res['held_events'])} file(s) stay held "
                                "until a corrected mapping is approved.</p>")

    async def review_approve(request: Request):
        if not settings.review_secret:
            return page("Not configured", "<h2>Review links are not enabled.</h2>", 503)
        pid, dg, bad = token_or_page(request)
        if bad:
            return bad
        form = await request.form()
        who = str(form.get("approved_by", "")).strip()[:80]
        flags = [str(x) for x in form.getlist("accept")]
        if not who:
            return page("Name needed", "<h2>Please enter your name.</h2>", 400)

        def work():
            with db.connection() as c:
                return do_approve(c, pid, "email-link:" + who, flags, dg, digest_is_prefix=True)
        try:
            res = await run_in_threadpool(work)
        except (ProfileSpecError, ValueError, KeyError) as e:
            return page("Not approved", f"<h2>Not approved</h2><p>{review.html.escape(str(e)[:300])}</p>", 400)
        if res is None:
            return page("Changed", "<h2>This proposal changed after the link was sent. Ask for a fresh review.</h2>", 409)
        return page("Approved", f"<h2>Approved</h2><p>Mapping #{pid} is active. {res['requeued_events']} held file(s) "
                                "were queued and will be processed on the next run.</p>")

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

        def bundle():
            with db.connection() as c:
                return review_bundle(c, p.profile_id)
        rb = await run_in_threadpool(bundle)
        return _json({**p.__dict__, "digest": rb["digest"], "review_markdown": rb["review_markdown"],
                      "review_url": rb["review_url"], "email_subject": rb["email_subject"],
                      "email_text": rb["email_text"], "email_html": rb["email_html"],
                      "critical_changed": rb["critical_changed"]})

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
        Route("/v1/mapping/{profile_id:int}/reject", mapping_reject, methods=["POST"]),
        Route("/v1/mapping/{profile_id:int}/review", mapping_review),
        Route("/review/{token}/reject", review_reject, methods=["POST"]),
        Route("/review/{token}", review_page),
        Route("/review/{token}/approve", review_approve, methods=["POST"]),
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
