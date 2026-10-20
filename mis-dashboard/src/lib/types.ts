/** Shared request/response types. These ARE the API contract (see docs/API_CONTRACT.md). */

export type DateField = "entry" | "disbursement" | "report";
export type DatePreset = "today" | "yesterday" | "last7" | "thisMonth" | "prevMonth" | "custom";
export type AttributionStatus = "resolved" | "unregistered_code" | "organic" | "unresolved";
export type SortDir = "asc" | "desc";

export const SORT_KEYS = [
  "externalLeadId", "attributionCode", "portalName", "status", "leadEntryDate", "disbursedDate",
  "requestedLoanAmount", "approvedAmount", "disbursedAmount", "payoutAmount", "state", "city",
] as const;
export type SortKey = (typeof SORT_KEYS)[number];

/** Filters applied to BOTH the summary and the lead list (and the export). */
export interface QueryFilters {
  code: string;                       // '' = all partners; otherwise 3 chars [0-9A-Z], kept as text
  dateField: DateField;
  from: string;                       // YYYY-MM-DD, '' when no date filter
  to: string;
  statuses: string[];                 // lead_status codes
  portals: string[];                  // reporting portal codes
  states: string[];                   // lead state names (as listed in Meta.states)
  attributionStatuses: AttributionStatus[];
}

export interface LeadsQuery extends QueryFilters {
  page: number;                       // 1-based
  pageSize: number;
  sortKey: SortKey;
  sortDir: SortDir;
}

export interface LeadRow {
  leadId: string;                     // internal surrogate id, string-typed
  externalLeadId: string;
  idNamespace: string;
  attributionCode: string | null;
  attributionStatus: AttributionStatus;
  partnerName: string | null;
  portalCode: string;
  portalName: string;
  status: string;                     // canonical status code
  statusRaw: string | null;
  statusReason: string | null;
  leadEntryDate: string | null;       // YYYY-MM-DD
  disbursedDate: string | null;
  reportDate: string | null;          // report date of the newest file that touched the lead
  requestedLoanAmount: number | null; // INR, null = not reported
  approvedAmount: number | null;
  disbursedAmount: number | null;
  payoutAmount: number | null;
  state: string | null;
  city: string | null;
  employmentType: string | null;
  leadSource: string | null;
  lastObservedAt: string | null;      // ISO timestamp
}

export interface LeadsPage {
  items: LeadRow[];
  total: number;                      // all rows matching the filters
  page: number;
  pageSize: number;
}

export interface Summary {
  totalLeads: number;
  disbursedCount: number;
  disbursedAmount: number | null;     // null = no disbursed lead has an amount reported
  disbursedMissingAmountCount: number;
  payout: number | null;              // null = no lead in the set has a payout reported
  approvedCount: number;
  rejectedCount: number;
  conversionRatePct: number | null;   // disbursedCount / totalLeads * 100; null when totalLeads = 0
  generatedAt: string;
}

export interface StatusOption { code: string; label: string }
export interface PortalOption { code: string; name: string }

export interface Meta {
  mode: "demo" | "live";
  statuses: StatusOption[];
  portals: PortalOption[];
  states: string[];                   // distinct states present in the data
  /** Real freshness info; null when the backend cannot say. NEVER fabricated by the UI. */
  sync: { dataThroughReportDate: string | null; lastLoadAt: string | null } | null;
  definitions: { approvedStatuses: string[]; disbursedStatuses: string[]; rejectedStatuses: string[] };
}

export interface PartnerLookup {
  code: string;
  state: "resolved" | "unregistered" | "unknown";
  partnerName: string | null;
}

export type ExportFormat = "csv" | "xlsx";

export interface ExportResult { blob: Blob; filename: string; rowCount: number | null }

export interface ApiErrorBody { error: { code: string; message: string; details?: string[] } }
