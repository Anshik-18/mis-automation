# Lead MIS dashboard

Next.js (App Router) + TypeScript + Tailwind CSS v4. Search by attribution code, filter by date (with an explicit date basis),
review summary metrics, inspect leads, export every matching row to CSV or Excel.

> **Status:** UI, API client, demo data service and BFF are implemented and tested in a sandbox (see *Tests*). It has **not**
> been run under `next dev` / `next build` yet, and it has **not** been run against the real database: the backend endpoints
> it needs do not exist yet (see `docs/API_CONTRACT.md`). Demo mode shows a permanent "synthetic data" badge.

## Run locally
```bash
cd mis-dashboard
npm install
cp .env.example .env.local        # MIS_DATA_SOURCE=demo is fine for local UI work
npm run dev                       # http://localhost:3000
```
`MIS_DATA_SOURCE`: `demo` (synthetic data; default only when NODE_ENV≠production) or `live` (needs `MIS_API_BASE_URL` and
`MIS_API_KEY`, server-side only — never `NEXT_PUBLIC_`). In production the source must be set explicitly.

## Layout
| Path | What |
|---|---|
| `app/page.tsx`, `app/layout.tsx`, `app/globals.css` | Next entry points |
| `app/api/mis/[...path]/route.ts` | BFF route → `src/server/handlers.ts` |
| `src/components/` | `Dashboard`, `FilterPanel`, `ActiveFilters`, `MetricCards`, `LeadsTable`, `Pagination`, `ExportMenu`, `StatusBadge`, `Header`, `ui` |
| `src/client/` | typed API client + response guards (`api.ts`), state hook (`useDashboard.ts`), `useDebounced` |
| `src/lib/` | types/contract, dates (IST), attribution validation, INR/count formatting, filter ↔ query/URL, CSV, XLSX + ZIP writers |
| `src/server/` | demo service + synthetic data, live proxy (adds the API key server-side), config, handlers |
| `tests/` | unit/API tests (`*.test.ts`) and browser tests (`e2e/`) |
| `docs/API_CONTRACT.md` | endpoints, parameters, metric definitions, what the backend must implement |

Switching from demo to the real API is a config change: the live path is a validating proxy to `{MIS_API_BASE_URL}/v1/mis/*`.

## Behaviour notes
* Attribution code is text, upper-cased and validated (`^[0-9A-Z]{3}$`); `027` is never turned into `27`.
* Date presets are computed in **Asia/Kolkata**. The date basis (Lead Entry / Disbursement / Report) is explicit, shown beside the
  results, sent to the API and written into the Excel `Summary` sheet. CSV carries it in the file name.
* Metrics and table are fetched from the same applied filters; a newer request aborts older ones, so stale responses never win.
* Filters are in the URL (`?code=027&range=last7&dateField=entry…`) and survive reload; invalid parameters fall back to defaults.
* Export always covers **all** matching rows (server-side), shows a busy state, and reports success only after a non-empty file
  was received and handed to the browser.
* Conversion rate = distinct leads with status Disbursed ÷ distinct leads in the filtered set × 100 (shown under the cards).
* "Approved" = status Approved or Disbursement pending — a business definition to confirm (`docs/API_CONTRACT.md`).

## Tests
```bash
npm run typecheck      # tsc --noEmit (needs @types/react, installed by npm install)
npm test               # unit + API + export tests (tsx --test). Needs Python with openpyxl on PATH: used only to verify
                       # CSV/XLSX output independently:  pip install openpyxl
npx playwright install chromium
npm run test:e2e       # builds the real components with esbuild and drives them in Chromium (26 scenarios)
```
`test:e2e` is a harness that bundles the components itself (it does not start Next.js).

## Security
* The browser never sees the backend URL or API key (`tests/e2e` asserts the bundle contains neither).
* Only `GET`, an allow-listed set of paths, and validated parameters are forwarded; upstream error text is not relayed.
* **User authentication is not implemented here.** Put the app behind your SSO / reverse proxy, or add auth in the BFF, before
  exposing it with live data.
* CSV exports neutralise spreadsheet formulas; XLSX stores text as text.
