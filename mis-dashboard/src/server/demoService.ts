import { csvRow, EXPORT_COLUMN_WIDTHS, EXPORT_HEADERS, summaryRows, xlsxRow } from "../lib/exportColumns";
import { toCsv } from "../lib/csv";
import { buildXlsx, XLSX_MAX_DATA_ROWS } from "../lib/xlsx";
import { todayInTz, formatDateTime } from "../lib/dates";
import type { ExportFormat, LeadRow, LeadsPage, LeadsQuery, Meta, PartnerLookup, QueryFilters, SortKey, Summary } from "../lib/types";
import {
  DEMO_DEFINITIONS, DEMO_PARTNERS, DEMO_PORTALS, DEMO_STATUSES, DEMO_UNREGISTERED_CODE, generateDemoLeads,
} from "./demoData";
import { HttpError } from "./errors";

export interface ExportFile { body: Buffer | string; contentType: string; filename: string; rowCount: number }

export interface MisService {
  meta(): Promise<Meta>;
  summary(f: QueryFilters): Promise<Summary>;
  leads(q: LeadsQuery): Promise<LeadsPage>;
  partner(code: string): Promise<PartnerLookup>;
  exportFile(q: LeadsQuery, format: ExportFormat): Promise<ExportFile>;
}

const dateOf = (r: LeadRow, f: QueryFilters["dateField"]) =>
  f === "entry" ? r.leadEntryDate : f === "disbursement" ? r.disbursedDate : r.reportDate;

export function matches(r: LeadRow, f: QueryFilters): boolean {
  if (f.code && r.attributionCode !== f.code) return false;
  if (f.from || f.to) {
    const d = dateOf(r, f.dateField);
    if (!d || d < f.from || d > f.to) return false;           // a lead without that date never matches a date filter
  }
  if (f.statuses.length && !f.statuses.includes(r.status)) return false;
  if (f.portals.length && !f.portals.includes(r.portalCode)) return false;
  if (f.states.length && !(r.state && f.states.includes(r.state))) return false;
  if (f.attributionStatuses.length && !f.attributionStatuses.includes(r.attributionStatus)) return false;
  return true;
}

export function summarize(rows: LeadRow[], now: Date): Summary {
  const d = DEMO_DEFINITIONS;
  let disbursedCount = 0, approvedCount = 0, rejectedCount = 0, missingAmt = 0;
  let disbursedSum = 0, disbursedKnown = 0, payoutSum = 0, payoutKnown = 0;
  for (const r of rows) {
    if (d.disbursedStatuses.includes(r.status)) {
      disbursedCount++;
      if (r.disbursedAmount === null) missingAmt++; else { disbursedSum += r.disbursedAmount; disbursedKnown++; }
    }
    if (d.approvedStatuses.includes(r.status)) approvedCount++;
    if (d.rejectedStatuses.includes(r.status)) rejectedCount++;
    if (r.payoutAmount !== null) { payoutSum += r.payoutAmount; payoutKnown++; }
  }
  const total = rows.length;
  return {
    totalLeads: total, disbursedCount,
    disbursedAmount: disbursedKnown ? Math.round(disbursedSum * 100) / 100 : null,
    disbursedMissingAmountCount: missingAmt,
    payout: payoutKnown ? Math.round(payoutSum * 100) / 100 : null,
    approvedCount, rejectedCount,
    conversionRatePct: total ? Math.round((disbursedCount / total) * 10000) / 100 : null,
    generatedAt: now.toISOString(),
  };
}

function cmp(a: LeadRow, b: LeadRow, key: SortKey, dir: 1 | -1): number {
  const av = a[key], bv = b[key];
  if (av === null && bv === null) return 0;
  if (av === null) return 1;                                   // nulls last in both directions
  if (bv === null) return -1;
  const c = typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv), "en", { numeric: true });
  return c * dir;
}

function sorted(rows: LeadRow[], key: SortKey, dirName: "asc" | "desc"): LeadRow[] {
  const dir = dirName === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => cmp(a, b, key, dir) || Number(a.leadId) - Number(b.leadId));   // stable tie-breaker
}

function safeName(s: string): string { return s.replace(/[^A-Za-z0-9._-]/g, "_"); }

export function createDemoService(opts: { now?: () => Date; size?: number } = {}): MisService {
  const nowFn = opts.now ?? (() => new Date());
  const today = todayInTz(nowFn());
  const all = generateDemoLeads(today, opts.size ?? 6000);

  const select = (f: QueryFilters) => all.filter((r) => matches(r, f));
  const partnerName = (code: string) => (code ? DEMO_PARTNERS[code] ?? null : null);

  return {
    async meta() {
      return { mode: "demo", statuses: DEMO_STATUSES, portals: DEMO_PORTALS, states: [...new Set(all.map((r) => r.state).filter((s): s is string => !!s))].sort(), sync: null, definitions: DEMO_DEFINITIONS };
    },
    async summary(f) { return summarize(select(f), nowFn()); },
    async leads(q) {
      const rows = sorted(select(q), q.sortKey, q.sortDir);
      const start = (q.page - 1) * q.pageSize;
      return { items: rows.slice(start, start + q.pageSize), total: rows.length, page: q.page, pageSize: q.pageSize };
    },
    async partner(code) {
      if (DEMO_PARTNERS[code]) return { code, state: "resolved", partnerName: DEMO_PARTNERS[code] };
      if (code === DEMO_UNREGISTERED_CODE) return { code, state: "unregistered", partnerName: null };
      return { code, state: "unknown", partnerName: null };
    },
    async exportFile(q, format) {
      const rows = sorted(select(q), q.sortKey, q.sortDir);
      const now = nowFn();
      const base = `lead-mis_${q.code || "all"}_${q.dateField}_${q.from || "any"}_${q.to || "any"}`;
      if (format === "csv") {
        return { body: toCsv(EXPORT_HEADERS, rows.map(csvRow)), contentType: "text/csv; charset=utf-8", filename: `${safeName(base)}.csv`, rowCount: rows.length };
      }
      if (rows.length > XLSX_MAX_DATA_ROWS) {
        throw new HttpError(422, "export_too_large", "Too many rows for an Excel sheet. Export as CSV instead.");
      }
      const wb = buildXlsx([
        { name: "Summary", columnWidths: [34, 48], rows: summaryRows({
          generatedAt: `${now.toISOString()} (${formatDateTime(now.toISOString())})`,
          dataSource: "DEMO - synthetic data, not from a database", partnerName: partnerName(q.code),
          filters: q, summary: summarize(rows, now), rowCount: rows.length }) },
        { name: "Leads", columnWidths: EXPORT_COLUMN_WIDTHS, headerRow: true, rows: [EXPORT_HEADERS, ...rows.map(xlsxRow)] },
      ]);
      return { body: wb, contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename: `${safeName(base)}.xlsx`, rowCount: rows.length };
    },
  };
}
