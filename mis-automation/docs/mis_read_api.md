# MIS read API (`/v1/mis/*`)

Read-only endpoints for the Lead MIS dashboard. Spec: `mis-dashboard/docs/API_CONTRACT.md`. Code: `processor/mis_api.py`.
All require the same `X-API-Key` (or Bearer) as the rest of `/v1`. Errors use `{"error":{"code","message","details?"}}`.

| Endpoint | Notes |
|---|---|
| `GET /v1/mis/meta` | statuses (`lead_statuses`), active portals (`reporting_portals`), `sync` = max `report_date` / max `processed_at` of processed `source_files` (null if none), fixed metric definitions |
| `GET /v1/mis/summary` | one SQL aggregate over ALL matching leads |
| `GET /v1/mis/leads` | server-side pagination, allow-listed sort columns, `NULLS LAST`, tie-break `lead_id` |
| `GET /v1/mis/partners/{code}` | `resolved` (in `attribution_partners`) / `unregistered` (leads carry it) / `unknown` |
| `GET /v1/mis/export?format=csv\|xlsx` | all matching rows; CSV streamed from a server-side cursor; XLSX (Summary + Leads) built with openpyxl write-only mode, max 1,048,575 rows (else 422 `export_too_large`) |

Safety: parameterised SQL only (sort/date columns from closed allow-lists); each request runs in a READ ONLY, REPEATABLE READ transaction
(count, rows and summary of an export share one snapshot) with a statement timeout (30-60 s, exports 300 s); no customer free-text columns are selected;
CSV cells starting with `= + - @` are prefixed with `'`; XLSX strings are stored as text; database errors return only `{"error":{"code":"internal"}}`.

Not measured: performance on production-sized data. Existing indexes: `ix_leads_partner_entry`, `ix_leads_partner_disb`, `ix_leads_entry_portal`; a report-date filter joins `source_files` by primary key.
