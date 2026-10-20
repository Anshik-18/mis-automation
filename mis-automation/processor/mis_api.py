"""Read-only API for the Lead MIS dashboard (`/v1/mis/*`), as specified in mis-dashboard/docs/API_CONTRACT.md.

All queries are parameterised; identifiers (sort column, date column) come from closed allow-lists, never from input.
Every request runs in a READ ONLY transaction with a statement timeout. Metrics are computed in SQL over ALL matching
leads (one row per lead already), never over a page. No customer free-text (names, phones, e-mail, bank data) is selected.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .database import SEARCH_PATH_OPT

DEFINITIONS = {"approvedStatuses": ["approved", "disbursement_pending"], "disbursedStatuses": ["disbursed"],
               "rejectedStatuses": ["rejected"]}
PAGE_SIZES = (10, 25, 50, 100)
XLSX_MAX_ROWS = 1_048_575
CODE_RE = re.compile(r"^[0-9A-Z]{3}$")
TOKEN_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
STATE_RE = re.compile(r"^[A-Za-z][A-Za-z .&'-]{0,59}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ATTR = ("resolved", "unregistered_code", "organic", "unresolved")

# API field -> SQL expression (closed allow-list)
SORTS = {
    "externalLeadId": "l.external_lead_id", "attributionCode": "l.attribution_code", "portalName": "p.display_name",
    "status": "l.lead_status_code", "leadEntryDate": "l.lead_entry_date", "disbursedDate": "l.disbursed_date",
    "requestedLoanAmount": "l.requested_loan_amount", "approvedAmount": "l.approved_amount",
    "disbursedAmount": "l.disbursed_amount", "payoutAmount": "l.payout_amount", "state": "l.state", "city": "l.city",
}
DATE_EXPR = {"entry": "l.lead_entry_date", "disbursement": "l.disbursed_date", "report": "sf.report_date"}
DATE_LABEL = {"entry": "Lead Entry Date", "disbursement": "Disbursement Date", "report": "Report Date"}

FROM = """FROM mis.leads l
  JOIN mis.reporting_portals p ON p.portal_id = l.portal_id
  LEFT JOIN mis.attribution_partners ap ON ap.partner_id = l.attribution_partner_id
  LEFT JOIN mis.source_files sf ON sf.source_file_id = l.last_seen_file_id"""

LEAD_COLS = """l.lead_id, l.external_lead_id, l.id_namespace, l.attribution_code, l.attribution_status, ap.partner_name,
  p.portal_code, p.display_name, l.lead_status_code, l.lead_status_raw, l.status_reason, l.lead_entry_date,
  l.disbursed_date, sf.report_date, l.requested_loan_amount, l.approved_amount, l.disbursed_amount, l.payout_amount,
  l.state, l.city, l.employment_type, l.lead_source, l.last_observed_at"""


class MisApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _error(status: int, code: str, message: str, details=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, **({"details": details} if details else {})}}
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _ok(data) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


# ---- parameter parsing ------------------------------------------------------------------------------------

def _valid_date(s: str) -> bool:
    if not ISO_RE.match(s):
        return False
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return False
    return 2000 <= d.year <= 2100


def parse_filters(q) -> dict:
    errors: list[str] = []
    code = (q.get("code") or "").strip().upper()
    if code and not CODE_RE.match(code):
        errors.append("code: must be exactly 3 characters, digits or A-Z")
    date_field = q.get("dateField") or "entry"
    if date_field not in DATE_EXPR:
        errors.append("dateField must be entry, disbursement or report.")
    frm, to = q.get("from") or "", q.get("to") or ""
    if frm or to:
        if not (frm and to):
            errors.append("date range: provide both from and to.")
        elif not (_valid_date(frm) and _valid_date(to)):
            errors.append("date range: from/to must be valid YYYY-MM-DD dates.")
        elif to < frm:
            errors.append("date range: to cannot be earlier than from.")
    lst = lambda k: [s.strip() for s in (q.get(k) or "").split(",") if s.strip()]
    statuses, portals, attribution = lst("status"), lst("portal"), lst("attribution")
    if any(not TOKEN_RE.match(s) for s in statuses):
        errors.append("status contains an invalid value.")
    if any(not TOKEN_RE.match(s) for s in portals):
        errors.append("portal contains an invalid value.")
    states = lst("state")
    if any(not STATE_RE.match(x) for x in states):
        errors.append("state contains an invalid value.")
    if any(a not in ATTR for a in attribution):
        errors.append("attribution contains an invalid value.")
    if errors:
        raise MisApiError(400, "invalid_request", "Invalid filters.", errors)
    return {"code": code, "dateField": date_field, "from": frm, "to": to, "statuses": statuses, "portals": portals,
            "states": states, "attribution": attribution}


def parse_leads(q) -> dict:
    errors: list[str] = []
    try:
        f = parse_filters(q)
    except MisApiError as e:
        f, errors = {}, list(e.details or [])
    def integer(k, default):
        try:
            return int(q.get(k) or default)
        except ValueError:
            return -1
    page, size = integer("page", 1), integer("pageSize", 25)
    sort, direction = q.get("sort") or "leadEntryDate", q.get("dir") or "desc"
    if page < 1 or page > 1_000_000:
        errors.append("page must be a positive integer.")
    if size not in PAGE_SIZES:
        errors.append(f"pageSize must be one of {', '.join(map(str, PAGE_SIZES))}.")
    if sort not in SORTS:
        errors.append("sort is not a sortable column.")
    if direction not in ("asc", "desc"):
        errors.append("dir must be asc or desc.")
    if errors:
        raise MisApiError(400, "invalid_request", "Invalid query.", errors)
    return {**f, "page": page, "pageSize": size, "sort": sort, "dir": direction}


def where_clause(f: dict) -> tuple[str, list]:
    conds, params = [], []
    if f["code"]:
        conds.append("l.attribution_code = %s"); params.append(f["code"])
    if f["from"]:
        expr = DATE_EXPR[f["dateField"]]                     # NULL dates never satisfy BETWEEN
        conds.append(f"{expr} BETWEEN %s AND %s"); params += [f["from"], f["to"]]
    if f["statuses"]:
        conds.append("l.lead_status_code = ANY(%s)"); params.append(f["statuses"])
    if f["portals"]:
        conds.append("p.portal_code = ANY(%s)"); params.append(f["portals"])
    if f["states"]:
        conds.append("lower(l.state) = ANY(%s)"); params.append([x.lower() for x in f["states"]])
    if f["attribution"]:
        conds.append("l.attribution_status = ANY(%s)"); params.append(f["attribution"])
    return ("WHERE " + " AND ".join(conds)) if conds else "", params


# ---- queries ---------------------------------------------------------------------------------------------

def _money(v):
    return None if v is None else float(v)


def _iso(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return v.isoformat()


def row_to_lead(r) -> dict:
    return {"leadId": str(r[0]), "externalLeadId": r[1], "idNamespace": r[2], "attributionCode": r[3],
            "attributionStatus": r[4], "partnerName": r[5], "portalCode": r[6], "portalName": r[7], "status": r[8],
            "statusRaw": r[9], "statusReason": r[10], "leadEntryDate": _iso(r[11]), "disbursedDate": _iso(r[12]),
            "reportDate": _iso(r[13]), "requestedLoanAmount": _money(r[14]), "approvedAmount": _money(r[15]),
            "disbursedAmount": _money(r[16]), "payoutAmount": _money(r[17]), "state": r[18], "city": r[19],
            "employmentType": r[20], "leadSource": r[21], "lastObservedAt": _iso(r[22])}


def summary_query(conn, f: dict) -> dict:
    where, params = where_clause(f)
    d, a, rj = DEFINITIONS["disbursedStatuses"], DEFINITIONS["approvedStatuses"], DEFINITIONS["rejectedStatuses"]
    r = conn.execute(f"""
      SELECT count(*),
             count(*) FILTER (WHERE l.lead_status_code = ANY(%s)),
             sum(l.disbursed_amount) FILTER (WHERE l.lead_status_code = ANY(%s)),
             count(*) FILTER (WHERE l.lead_status_code = ANY(%s) AND l.disbursed_amount IS NULL),
             sum(l.payout_amount),
             count(*) FILTER (WHERE l.lead_status_code = ANY(%s)),
             count(*) FILTER (WHERE l.lead_status_code = ANY(%s))
      {FROM} {where}""", [d, d, d, a, rj] + params).fetchone()
    total, disbursed = r[0], r[1]
    return {"totalLeads": total, "disbursedCount": disbursed, "disbursedAmount": _money(r[2]),
            "disbursedMissingAmountCount": r[3], "payout": _money(r[4]), "approvedCount": r[5], "rejectedCount": r[6],
            "conversionRatePct": round(disbursed / total * 100, 2) if total else None,
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


def count_query(conn, f: dict) -> int:
    where, params = where_clause(f)
    return conn.execute(f"SELECT count(*) {FROM} {where}", params).fetchone()[0]


def order_by(q: dict) -> str:
    return f"ORDER BY {SORTS[q['sort']]} {'ASC' if q['dir'] == 'asc' else 'DESC'} NULLS LAST, l.lead_id ASC"


def leads_query(conn, q: dict) -> dict:
    where, params = where_clause(q)
    total = count_query(conn, q)
    rows = conn.execute(f"SELECT {LEAD_COLS} {FROM} {where} {order_by(q)} LIMIT %s OFFSET %s",
                        params + [q["pageSize"], (q["page"] - 1) * q["pageSize"]]).fetchall()
    return {"items": [row_to_lead(r) for r in rows], "total": total, "page": q["page"], "pageSize": q["pageSize"]}


# ---- export ----------------------------------------------------------------------------------------------

STATUS_LABEL = {"resolved": "Registered partner", "unregistered_code": "Unregistered code", "organic": "Organic",
                "unresolved": "Unresolved"}
EXPORT_COLUMNS = [  # (header, kind, lead-dict key)  - identical to mis-dashboard/src/lib/exportColumns.ts
    ("Lead ID", "text", "externalLeadId"), ("ID Namespace", "text", "idNamespace"),
    ("Attribution Code", "text", "attributionCode"), ("Partner", "text", "partnerName"),
    ("Attribution Status", "label", "attributionStatus"), ("Reporting Portal", "text", "portalName"),
    ("Lead Status", "text", "status"), ("Status As Reported", "text", "statusRaw"),
    ("Status Reason", "text", "statusReason"), ("Lead Entry Date", "date", "leadEntryDate"),
    ("Disbursement Date", "date", "disbursedDate"), ("Report Date", "date", "reportDate"),
    ("Requested Loan Amount (INR)", "money", "requestedLoanAmount"), ("Approved Amount (INR)", "money", "approvedAmount"),
    ("Disbursed Amount (INR)", "money", "disbursedAmount"), ("Payout (INR)", "money", "payoutAmount"),
    ("State", "text", "state"), ("City", "text", "city"), ("Employment Type", "text", "employmentType"),
    ("Lead Source", "text", "leadSource"), ("Last Observed At (UTC)", "text", "lastObservedAt"),
]


def _csv_cell(kind: str, v):
    if v is None:
        return ""
    if kind == "label":
        return STATUS_LABEL.get(v, v)
    if kind == "money":
        return f"{Decimal(str(v)):.2f}"
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s        # CSV-injection guard


def _export_name(q: dict, ext: str) -> str:
    base = f"lead-mis_{q['code'] or 'all'}_{q['dateField']}_{q['from'] or 'any'}_{q['to'] or 'any'}"
    return re.sub(r"[^A-Za-z0-9._-]", "_", base) + "." + ext


def _lead_dicts(conn, q: dict, batch: int = 2000):
    where, params = where_clause(q)
    with conn.cursor(name="mis_export") as cur:
        cur.itersize = batch
        cur.execute(f"SELECT {LEAD_COLS} {FROM} {where} {order_by(q)}", params)
        for r in cur:
            yield row_to_lead(r)


def csv_chunks(conn, q: dict):
    try:
        yield "﻿".encode("utf-8")
        buf = io.StringIO(newline="")
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow([h for h, _, _ in EXPORT_COLUMNS])
        n = 0
        for lead in _lead_dicts(conn, q):
            w.writerow([_csv_cell(kind, lead[key]) for _, kind, key in EXPORT_COLUMNS])
            n += 1
            if n % 500 == 0:
                yield buf.getvalue().encode("utf-8"); buf.seek(0); buf.truncate()
        yield buf.getvalue().encode("utf-8")
    finally:
        conn.rollback()
        conn.close()


def build_xlsx(conn, q: dict, summary: dict, partner_name: str | None, total: int) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill

    wb = Workbook(write_only=True)
    bold, head_fill = Font(bold=True), PatternFill("solid", fgColor="E8EEF7")
    s = wb.create_sheet("Summary")
    s.column_dimensions["A"].width = 34
    s.column_dimensions["B"].width = 48

    def text_cell(ws, v, font=None, fill=None):
        c = WriteOnlyCell(ws, value=v)
        c.data_type = "s"                                          # text, even if it starts with '=': never a formula
        if font: c.font = font
        if fill: c.fill = fill
        return c

    def kv(k, v):
        return [text_cell(s, k, bold), v if not isinstance(v, str) else text_cell(s, v)]
    now = datetime.now(timezone.utc)
    rows = [
        [text_cell(s, "Lead MIS export", bold)], kv("Generated at", now.isoformat()),
        kv("Data source", "MIS database (live)"), [], [text_cell(s, "Applied filters", bold)],
        kv("Attribution code", q["code"] or "All"), kv("Partner", partner_name or ("Not available" if q["code"] else "All")),
        kv("Date basis", DATE_LABEL[q["dateField"]]), kv("Date from", q["from"] or "Any"), kv("Date to", q["to"] or "Any"),
        kv("Lead status", ", ".join(q["statuses"]) or "All"), kv("Reporting portal", ", ".join(q["portals"]) or "All"),
        kv("State", ", ".join(q["states"]) or "All"),
        kv("Attribution status", ", ".join(q["attribution"]) or "All"), kv("Rows in Leads sheet", total), [],
        [text_cell(s, "Summary metrics (same filters)", bold)],
        kv("Total leads", summary["totalLeads"]), kv("Total disbursed count", summary["disbursedCount"]),
        kv("Total disbursed amount (INR)", summary["disbursedAmount"] if summary["disbursedAmount"] is not None else "Not reported"),
        kv("Total payout (INR)", summary["payout"] if summary["payout"] is not None else "Not reported"),
        kv("Total approved leads", summary["approvedCount"]), kv("Total rejected leads", summary["rejectedCount"]),
        kv("Conversion rate (%)", summary["conversionRatePct"] if summary["conversionRatePct"] is not None else "n/a"),
        [], [text_cell(s, "Conversion rate = distinct leads with status Disbursed / distinct leads in the filtered set x 100.")],
    ]
    for r in rows:
        s.append(r)

    ws = wb.create_sheet("Leads")
    ws.freeze_panes = "A2"
    ws.append([text_cell(ws, h, bold, head_fill) for h, _, _ in EXPORT_COLUMNS])
    for i, (_, kind, _) in enumerate(EXPORT_COLUMNS):
        ws.column_dimensions[chr(65 + i)].width = 14 if kind == "date" else 20 if kind == "money" else 22
    n = 0
    for lead in _lead_dicts(conn, q):
        out = []
        for _, kind, key in EXPORT_COLUMNS:
            v = lead[key]
            if v is None:
                out.append(None)
            elif kind == "date":
                c = WriteOnlyCell(ws, value=date.fromisoformat(v)); c.number_format = "yyyy-mm-dd"; out.append(c)
            elif kind == "money":
                c = WriteOnlyCell(ws, value=v); c.number_format = "#,##0.00"; out.append(c)
            elif kind == "label":
                out.append(text_cell(ws, STATUS_LABEL.get(v, v)))
            else:
                out.append(text_cell(ws, str(v)))
        ws.append(out)
        n += 1
    ws.auto_filter.ref = f"A1:{chr(64 + len(EXPORT_COLUMNS))}{max(n, 1) + 1}"
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


# ---- routes ----------------------------------------------------------------------------------------------

def build_routes(guarded, db, settings) -> list[Route]:
    """`guarded` is the app's API-key wrapper; MisApiError is mapped to the contract's error shape inside it."""

    def mis(fn):
        async def wrapper(request: Request):
            try:
                return await fn(request)
            except MisApiError as e:
                return _error(e.status, e.code, e.message, e.details)
        return guarded(wrapper)

    def read_conn(timeout_s: int):
        conn = psycopg.connect(db.url, options=SEARCH_PATH_OPT)
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ     # one snapshot for count + rows + summary
        conn.read_only = True
        conn.execute(f"SET LOCAL statement_timeout = {int(timeout_s) * 1000}")
        return conn

    def with_conn(timeout_s, fn):
        conn = read_conn(timeout_s)
        try:
            return fn(conn)
        finally:
            conn.rollback()
            conn.close()

    @mis
    async def meta(request: Request):
        def work(conn):
            statuses = [{"code": c, "label": n} for c, n in conn.execute(
                "SELECT status_code, display_name FROM mis.lead_statuses ORDER BY stage_order, status_code")]
            portals = [{"code": c, "name": n} for c, n in conn.execute(
                "SELECT portal_code, display_name FROM mis.reporting_portals WHERE is_active ORDER BY display_name")]
            rd, la = conn.execute("""SELECT max(report_date), max(processed_at) FROM mis.source_files
                                     WHERE processing_status = 'processed'""").fetchone()
            states = [r[0] for r in conn.execute("SELECT DISTINCT state FROM mis.leads WHERE state IS NOT NULL AND state <> '' ORDER BY state")]
            sync = {"dataThroughReportDate": _iso(rd), "lastLoadAt": _iso(la)} if (rd or la) else None
            return {"mode": "live", "statuses": statuses, "portals": portals, "states": states, "sync": sync, "definitions": DEFINITIONS}
        return _ok(await run_in_threadpool(with_conn, 30, work))

    @mis
    async def summary(request: Request):
        f = parse_filters(request.query_params)
        return _ok(await run_in_threadpool(with_conn, 60, lambda c: summary_query(c, f)))

    @mis
    async def leads(request: Request):
        q = parse_leads(request.query_params)
        return _ok(await run_in_threadpool(with_conn, 60, lambda c: leads_query(c, q)))

    @mis
    async def partner(request: Request):
        code = request.path_params["code"].strip().upper()
        if not CODE_RE.match(code):
            raise MisApiError(400, "invalid_request", "Invalid attribution code.")
        def work(conn):
            r = conn.execute("SELECT partner_name FROM mis.attribution_partners WHERE attribution_code = %s", (code,)).fetchone()
            if r:
                return {"code": code, "state": "resolved", "partnerName": r[0]}
            seen = conn.execute("SELECT 1 FROM mis.leads WHERE attribution_code = %s LIMIT 1", (code,)).fetchone()
            return {"code": code, "state": "unregistered" if seen else "unknown", "partnerName": None}
        return _ok(await run_in_threadpool(with_conn, 30, work))

    @mis
    async def export(request: Request):
        fmt = request.query_params.get("format")
        if fmt not in ("csv", "xlsx"):
            raise MisApiError(400, "invalid_request", "format must be csv or xlsx.")
        q = parse_leads(request.query_params)

        def prepare():
            conn = read_conn(300)
            try:
                total = count_query(conn, q)
                if fmt == "xlsx":
                    if total > XLSX_MAX_ROWS:
                        raise MisApiError(422, "export_too_large", "Too many rows for an Excel sheet. Export as CSV instead.")
                    summ = summary_query(conn, q)
                    pr = conn.execute("SELECT partner_name FROM mis.attribution_partners WHERE attribution_code=%s", (q["code"],)).fetchone() if q["code"] else None
                    body = build_xlsx(conn, q, summ, pr[0] if pr else None, total)
                    conn.rollback(); conn.close()
                    return total, body, None
                return total, None, conn                       # CSV: stream from the same snapshot; the generator closes it
            except BaseException:
                conn.close()
                raise
        total, body, conn = await run_in_threadpool(prepare)
        headers = {"Content-Disposition": f'attachment; filename="{_export_name(q, fmt)}"', "X-Export-Row-Count": str(total),
                   "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if fmt == "xlsx":
            return Response(body, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers=headers)
        return StreamingResponse(csv_chunks(conn, q), media_type="text/csv; charset=utf-8", headers=headers)

    return [
        Route("/v1/mis/meta", meta),
        Route("/v1/mis/summary", summary),
        Route("/v1/mis/leads", leads),
        Route("/v1/mis/partners/{code}", partner),
        Route("/v1/mis/export", export),
    ]
