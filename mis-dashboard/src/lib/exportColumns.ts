import { ATTRIBUTION_STATUS_LABEL, DATE_FIELD_LABEL } from "./constants";
import type { CsvValue } from "./csv";
import type { XCell } from "./xlsx";
import type { LeadRow, QueryFilters, Summary } from "./types";

type Kind = "text" | "date" | "money";
interface Col { header: string; kind: Kind; get: (r: LeadRow) => string | number | null }

export const EXPORT_COLUMNS: Col[] = [
  { header: "Lead ID", kind: "text", get: (r) => r.externalLeadId },
  { header: "ID Namespace", kind: "text", get: (r) => r.idNamespace },
  { header: "Attribution Code", kind: "text", get: (r) => r.attributionCode },
  { header: "Partner", kind: "text", get: (r) => r.partnerName },
  { header: "Attribution Status", kind: "text", get: (r) => ATTRIBUTION_STATUS_LABEL[r.attributionStatus] },
  { header: "Reporting Portal", kind: "text", get: (r) => r.portalName },
  { header: "Lead Status", kind: "text", get: (r) => r.status },
  { header: "Status As Reported", kind: "text", get: (r) => r.statusRaw },
  { header: "Status Reason", kind: "text", get: (r) => r.statusReason },
  { header: "Lead Entry Date", kind: "date", get: (r) => r.leadEntryDate },
  { header: "Disbursement Date", kind: "date", get: (r) => r.disbursedDate },
  { header: "Report Date", kind: "date", get: (r) => r.reportDate },
  { header: "Requested Loan Amount (INR)", kind: "money", get: (r) => r.requestedLoanAmount },
  { header: "Approved Amount (INR)", kind: "money", get: (r) => r.approvedAmount },
  { header: "Disbursed Amount (INR)", kind: "money", get: (r) => r.disbursedAmount },
  { header: "Payout (INR)", kind: "money", get: (r) => r.payoutAmount },
  { header: "State", kind: "text", get: (r) => r.state },
  { header: "City", kind: "text", get: (r) => r.city },
  { header: "Employment Type", kind: "text", get: (r) => r.employmentType },
  { header: "Lead Source", kind: "text", get: (r) => r.leadSource },
  { header: "Last Observed At (UTC)", kind: "text", get: (r) => r.lastObservedAt },
];

export const EXPORT_HEADERS = EXPORT_COLUMNS.map((c) => c.header);

export function csvRow(r: LeadRow): CsvValue[] {
  return EXPORT_COLUMNS.map((c) => {
    const v = c.get(r);
    return c.kind === "money" && typeof v === "number" ? v.toFixed(2) : v;
  });
}

export function xlsxRow(r: LeadRow): XCell[] {
  return EXPORT_COLUMNS.map((c): XCell => {
    const v = c.get(r);
    if (v === null) return null;
    if (c.kind === "date") return typeof v === "string" ? { kind: "date", iso: v } : null;
    if (c.kind === "money") return typeof v === "number" ? { kind: "money", v } : null;
    return v;
  });
}

export const EXPORT_COLUMN_WIDTHS = EXPORT_COLUMNS.map((c) => (c.kind === "date" ? 14 : c.kind === "money" ? 20 : 22));

export interface ExportContext {
  generatedAt: string;
  dataSource: string;
  partnerName: string | null;
  filters: QueryFilters;
  summary: Summary;
  rowCount: number;
}

/** Rows of the Excel "Summary" sheet: the filters that produced the file, then the metrics for the SAME filtered set. */
export function summaryRows(c: ExportContext): XCell[][] {
  const f = c.filters;
  const kv = (k: string, v: string | number | null): XCell[] => [{ kind: "bold", v: k }, v === null ? "" : v];
  return [
    [{ kind: "bold", v: "Lead MIS export" }],
    kv("Generated at", c.generatedAt),
    kv("Data source", c.dataSource),
    [],
    [{ kind: "bold", v: "Applied filters" }],
    kv("Attribution code", f.code || "All"),
    kv("Partner", c.partnerName ?? (f.code ? "Not available" : "All")),
    kv("Date basis", DATE_FIELD_LABEL[f.dateField]),
    kv("Date from", f.from || "Any"),
    kv("Date to", f.to || "Any"),
    kv("Lead status", f.statuses.length ? f.statuses.join(", ") : "All"),
    kv("Reporting portal", f.portals.length ? f.portals.join(", ") : "All"),
    kv("State", f.states.length ? f.states.join(", ") : "All"),
    kv("Attribution status", f.attributionStatuses.length ? f.attributionStatuses.join(", ") : "All"),
    kv("Rows in Leads sheet", c.rowCount),
    [],
    [{ kind: "bold", v: "Summary metrics (same filters)" }],
    ["Total leads", { kind: "int", v: c.summary.totalLeads }],
    ["Total disbursed count", { kind: "int", v: c.summary.disbursedCount }],
    ["Total disbursed amount (INR)", c.summary.disbursedAmount === null ? "Not reported" : { kind: "money", v: c.summary.disbursedAmount }],
    ["Total payout (INR)", c.summary.payout === null ? "Not reported" : { kind: "money", v: c.summary.payout }],
    ["Total approved leads", { kind: "int", v: c.summary.approvedCount }],
    ["Total rejected leads", { kind: "int", v: c.summary.rejectedCount }],
    ["Conversion rate", c.summary.conversionRatePct === null ? "n/a" : { kind: "pct", v: c.summary.conversionRatePct }],
    [],
    ["Conversion rate = distinct leads with status Disbursed ÷ distinct leads in the filtered set × 100."],
  ];
}
