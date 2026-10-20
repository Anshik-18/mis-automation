import { leadsQueryToParams, filtersToParams } from "../lib/filters";
import type {
  ExportFormat, ExportResult, LeadRow, LeadsPage, LeadsQuery, Meta, PartnerLookup, QueryFilters, Summary,
} from "../lib/types";

export type ApiErrorKind = "network" | "aborted" | "invalid_request" | "not_configured" | "unavailable" | "server" | "unexpected_shape";

export class ApiError extends Error {
  constructor(public kind: ApiErrorKind, message: string, public status?: number, public details?: string[]) { super(message); }
}

// ---- response guards: the UI never trusts the shape of what the backend returned --------------------------

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const numOrNull = (v: unknown) => v === null || (typeof v === "number" && Number.isFinite(v));
const strOrNull = (v: unknown) => v === null || typeof v === "string";
const shape = (what: string) => new ApiError("unexpected_shape", `The server returned an unexpected ${what} response.`);

export function asSummary(v: unknown): Summary {
  if (!isObj(v)) throw shape("summary");
  const ok = ["totalLeads", "disbursedCount", "approvedCount", "rejectedCount", "disbursedMissingAmountCount"].every((k) => typeof v[k] === "number" && Number.isFinite(v[k] as number))
    && numOrNull(v.disbursedAmount) && numOrNull(v.payout) && numOrNull(v.conversionRatePct);
  if (!ok) throw shape("summary");
  return v as unknown as Summary;
}

function asLead(v: unknown): LeadRow {
  if (!isObj(v)) throw shape("lead");
  const strs = ["leadId", "externalLeadId", "idNamespace", "attributionStatus", "portalCode", "portalName", "status"];
  const nulls = ["attributionCode", "partnerName", "statusRaw", "statusReason", "leadEntryDate", "disbursedDate", "reportDate", "state", "city", "employmentType", "leadSource", "lastObservedAt"];
  const nums = ["requestedLoanAmount", "approvedAmount", "disbursedAmount", "payoutAmount"];
  if (!strs.every((k) => typeof v[k] === "string") || !nulls.every((k) => strOrNull(v[k])) || !nums.every((k) => numOrNull(v[k]))) throw shape("lead");
  return v as unknown as LeadRow;
}

export function asLeadsPage(v: unknown): LeadsPage {
  if (!isObj(v) || !Array.isArray(v.items) || typeof v.total !== "number" || typeof v.page !== "number" || typeof v.pageSize !== "number") throw shape("leads");
  return { items: v.items.map(asLead), total: v.total, page: v.page, pageSize: v.pageSize };
}

export function asMeta(v: unknown): Meta {
  if (!isObj(v) || (v.mode !== "demo" && v.mode !== "live") || !Array.isArray(v.statuses) || !Array.isArray(v.portals) || !isObj(v.definitions)) throw shape("meta");
  const okS = v.statuses.every((s) => isObj(s) && typeof s.code === "string" && typeof s.label === "string");
  const okP = v.portals.every((p) => isObj(p) && typeof p.code === "string" && typeof p.name === "string");
  const states = v.states === undefined ? [] : v.states;
  if (!Array.isArray(states) || !states.every((s) => typeof s === "string")) throw shape("meta");
  const sync = v.sync;
  const okSync = sync === null || sync === undefined || (isObj(sync) && strOrNull(sync.dataThroughReportDate ?? null) && strOrNull(sync.lastLoadAt ?? null));
  if (!okS || !okP || !okSync) throw shape("meta");
  return { ...(v as unknown as Meta), states: states as string[], sync: (sync ?? null) as Meta["sync"] };
}

export function asPartner(v: unknown): PartnerLookup {
  if (!isObj(v) || typeof v.code !== "string" || !["resolved", "unregistered", "unknown"].includes(String(v.state)) || !strOrNull(v.partnerName ?? null)) throw shape("partner");
  return { code: v.code, state: v.state as PartnerLookup["state"], partnerName: (v.partnerName ?? null) as string | null };
}

// ---- client -----------------------------------------------------------------------------------------------

export interface ApiClient {
  meta(signal?: AbortSignal): Promise<Meta>;
  summary(f: QueryFilters, signal?: AbortSignal): Promise<Summary>;
  leads(q: LeadsQuery, signal?: AbortSignal): Promise<LeadsPage>;
  partner(code: string, signal?: AbortSignal): Promise<PartnerLookup>;
  exportFile(q: LeadsQuery, format: ExportFormat, signal?: AbortSignal): Promise<ExportResult>;
}

export function filenameFromDisposition(h: string | null, fallback: string): string {
  const m = h && /filename="?([^";]+)"?/i.exec(h);
  const raw = m ? m[1] : fallback;
  return raw.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 120) || fallback;
}

export function createApiClient(fetchImpl: typeof fetch = (...a) => fetch(...a), base = "/api/mis"): ApiClient {
  async function get(path: string, params: URLSearchParams | null, signal?: AbortSignal): Promise<Response> {
    let res: Response;
    try {
      res = await fetchImpl(`${base}/${path}${params ? "?" + params.toString() : ""}`, { method: "GET", headers: { accept: "application/json, */*" }, signal, credentials: "same-origin" });
    } catch (e) {
      if ((e as { name?: string })?.name === "AbortError" || signal?.aborted) throw new ApiError("aborted", "Request cancelled.");
      throw new ApiError("network", "Could not reach the server. Check your connection and try again.");
    }
    if (res.ok) return res;
    let body: { error?: { code?: string; message?: string; details?: string[] } } | null = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    const msg = body?.error?.message;
    const details = Array.isArray(body?.error?.details) ? body!.error!.details : undefined;
    if (res.status === 400 || res.status === 422) throw new ApiError("invalid_request", msg ?? "The request was not valid.", res.status, details);
    if (res.status === 503 && body?.error?.code === "not_configured") throw new ApiError("not_configured", msg ?? "The data source is not configured.", res.status);
    if (res.status === 502 || res.status === 503 || res.status === 504) throw new ApiError("unavailable", msg ?? "The data service is unavailable. Try again shortly.", res.status);
    throw new ApiError("server", msg ?? "The server reported an error.", res.status);
  }
  async function getJson<T>(path: string, params: URLSearchParams | null, parse: (v: unknown) => T, signal?: AbortSignal): Promise<T> {
    const res = await get(path, params, signal);
    let data: unknown;
    try { data = await res.json(); } catch (e) {
      if (signal?.aborted) throw new ApiError("aborted", "Request cancelled.");
      throw shape("JSON");
    }
    return parse(data);
  }
  return {
    meta: (signal) => getJson("meta", null, asMeta, signal),
    summary: (f, signal) => getJson("summary", filtersToParams(f), asSummary, signal),
    leads: (q, signal) => getJson("leads", leadsQueryToParams(q), asLeadsPage, signal),
    partner: (code, signal) => getJson(`partners/${encodeURIComponent(code)}`, null, asPartner, signal),
    async exportFile(q, format, signal) {
      const p = leadsQueryToParams(q); p.set("format", format);
      const res = await get("export", p, signal);
      let blob: Blob;
      try { blob = await res.blob(); } catch { throw signal?.aborted ? new ApiError("aborted", "Request cancelled.") : new ApiError("network", "The download was interrupted."); }
      if (blob.size === 0) throw new ApiError("unexpected_shape", "The server returned an empty file.");
      const n = Number(res.headers.get("x-export-row-count"));
      return { blob, filename: filenameFromDisposition(res.headers.get("content-disposition"), `lead-mis.${format}`), rowCount: Number.isInteger(n) && res.headers.has("x-export-row-count") ? n : null };
    },
  };
}
