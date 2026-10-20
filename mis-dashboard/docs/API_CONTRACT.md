# Lead MIS — API contract

The browser only talks to **`/api/mis/*`** on the Next.js server (a thin backend-for-frontend). The BFF either serves
**synthetic demo data** (`MIS_DATA_SOURCE=demo`, development only) or forwards each request to the real MIS backend at
`{MIS_API_BASE_URL}/v1/mis/*` with the API key added **server-side** (`MIS_DATA_SOURCE=live`). The backend therefore has to
implement the same paths and shapes under `/v1/mis/`. All endpoints are `GET`; every response is `Cache-Control: no-store`.

Types live in `src/lib/types.ts` (single source of truth); the browser validates every response shape at runtime
(`src/client/api.ts`) and shows an error state rather than rendering something unexpected.

## Common filter parameters

| Param | Meaning |
|---|---|
| `code` | Attribution code: exactly 3 chars `[0-9A-Z]`, **text** (`027` ≠ `27`). Omitted = all partners. Backend must match case-insensitively on the normalised upper-case value. |
| `dateField` | `entry` (leads.lead_entry_date) · `disbursement` (leads.disbursed_date) · `report` (report date of the newest file that touched the lead, `source_files.report_date` via `last_seen_file_id`). Default `entry`. Never combined. Leads with no value for the chosen field do not match a date filter. |
| `from`, `to` | Inclusive `YYYY-MM-DD` calendar dates (business timezone Asia/Kolkata). Both or neither. `to >= from`. |
| `status` | Comma list of canonical `lead_status` codes (`disbursed,rejected`). |
| `portal` | Comma list of reporting-portal codes. (Portal ≠ partner.) |
| `attribution` | Comma list of `resolved,unregistered_code,organic,unresolved`. |

Invalid input → `400 {"error":{"code":"invalid_request","message":"…","details":["…"]}}`.

## Endpoints

### `GET /meta` → `Meta`
`{ mode: "demo"|"live", statuses:[{code,label}], portals:[{code,name}], sync: {dataThroughReportDate, lastLoadAt}|null, definitions:{approvedStatuses, disbursedStatuses, rejectedStatuses} }`
`sync` must be **real** (e.g. newest processed `report_date` and last committed file time) or `null`; the header only shows what is returned.

### `GET /summary?<filters>` → `Summary`
```
{ totalLeads, disbursedCount, disbursedAmount|null, disbursedMissingAmountCount, payout|null,
  approvedCount, rejectedCount, conversionRatePct|null, generatedAt }
```
Computed over **all** leads matching the filters (never one page), counting each lead once (`leads` is already one row per
`(portal, id_namespace, external_lead_id)`), so duplicate report rows cannot double-count amounts.

| Metric | Definition |
|---|---|
| totalLeads | distinct leads matching the filters |
| disbursedCount | leads whose **status** is in `definitions.disbursedStatuses` (`disbursed`). An amount alone never makes a lead disbursed. |
| disbursedAmount | `SUM(disbursed_amount)` over those disbursed leads; `null` if none has an amount (shown "Not reported", distinct from ₹0) |
| disbursedMissingAmountCount | disbursed leads with `disbursed_amount IS NULL` |
| payout | `SUM(payout_amount)` over the filtered leads; `null` if none reported |
| approvedCount | status in `definitions.approvedStatuses` (`approved`, `disbursement_pending`) — **business definition to confirm** |
| rejectedCount | status in `definitions.rejectedStatuses` (`rejected`) |
| conversionRatePct | `disbursedCount / totalLeads × 100`; `null` when `totalLeads = 0` |

### `GET /leads?<filters>&page&pageSize&sort&dir` → `LeadsPage`
`{ items: LeadRow[], total, page, pageSize }` — `page` ≥ 1; `pageSize` ∈ {10,25,50,100}; `dir` ∈ {asc,desc};
`sort` ∈ `externalLeadId, attributionCode, portalName, status, leadEntryDate, disbursedDate, requestedLoanAmount, approvedAmount, disbursedAmount, payoutAmount, state, city`.
NULLs sort last in both directions; ties break on the surrogate lead id so paging is stable. `total` counts every matching lead.
`LeadRow` fields: see `src/lib/types.ts` (amounts are JSON numbers in INR with ≤ 2 decimals, `null` = not reported; dates `YYYY-MM-DD`).
Free-text customer fields (names, phones, e-mail, bank data) are **not** part of the row and must not be added.

### `GET /partners/{code}` → `PartnerLookup`
`{ code, state: "resolved"|"unregistered"|"unknown", partnerName|null }` — `resolved`: code is registered to a partner;
`unregistered`: leads carry the code but no partner is registered; `unknown`: neither.

### `GET /export?<filters>&sort&dir&format=csv|xlsx`
Streams a file containing **every** matching lead (ignores `page`/`pageSize`).
Headers: `Content-Type`, `Content-Disposition: attachment; filename="lead-mis_<code|all>_<dateField>_<from>_<to>.<ext>"`, `X-Export-Row-Count: <n>`.
* **CSV** — UTF-8 with BOM, CRLF, RFC 4180 quoting; text cells that begin with `= + - @ TAB CR` are prefixed with `'` (CSV-injection guard). One header row; columns = `EXPORT_COLUMNS` in `src/lib/exportColumns.ts`.
* **XLSX** — sheet `Summary` (generation time, data source, attribution code, partner, **date basis**, from/to, status/portal/attribution filters, row count, then the same summary metrics for the same filters) and sheet `Leads` (all rows; real dates and numbers; frozen, filtered header). Strings are stored as text, never formulas.
* More than 1,048,575 rows for XLSX → `422 {"error":{"code":"export_too_large", …}}` (use CSV).
* Large exports should be generated server-side by the backend; the BFF streams the body through unchanged.

## Errors
`{"error":{"code","message","details?"}}`. The BFF maps: backend 5xx / unreachable / 401 / 403 → `502`
(`upstream_error`, `upstream_unreachable`) **without** relaying backend messages; backend 4xx validation errors pass through.
`503 not_configured` when `MIS_DATA_SOURCE` is unset in production or `live` lacks its URL/key.

## Backend implementation status
Implemented in `mis-automation/processor/mis_api.py` (see `mis-automation/docs/mis_read_api.md`): `/v1/mis/{meta,summary,leads,partners/{code},export}`,
API-key protected, read-only PostgreSQL queries. Verified with integration tests against PostgreSQL and a cross-check through this app's
live proxy and response guards (not yet exercised through `next dev` against the Docker stack).
Decisions taken in the backend: `sync.lastLoadAt` = latest `processed_at` of successfully processed files (not `created_at`), and a
lead's report date is `source_files.report_date` of the file in `leads.last_seen_file_id` (range-style files without a report date never match a report-date filter).
