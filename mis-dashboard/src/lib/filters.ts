import { DEFAULT_PAGE_SIZE, DEFAULT_PRESET, PAGE_SIZES } from "./constants";
import { checkCode } from "./attribution";
import { resolvePreset, validateRange } from "./dates";
import {
  SORT_KEYS, type AttributionStatus, type DateField, type DatePreset, type LeadsQuery, type QueryFilters, type SortDir, type SortKey,
} from "./types";

const DATE_FIELDS: DateField[] = ["entry", "disbursement", "report"];
const ATTR: AttributionStatus[] = ["resolved", "unregistered_code", "organic", "unresolved"];
const TOKEN = /^[a-z0-9_-]{1,40}$/;
const STATE_RE = /^[A-Za-z][A-Za-z .&'-]{0,59}$/;
const PRESETS: DatePreset[] = ["today", "yesterday", "last7", "thisMonth", "prevMonth", "custom"];

export function emptyFilters(today: string, preset: Exclude<DatePreset, "custom"> = DEFAULT_PRESET as Exclude<DatePreset, "custom">): QueryFilters {
  return { code: "", dateField: "entry", ...resolvePreset(preset, today), statuses: [], portals: [], states: [], attributionStatuses: [] };
}

/** API query string for the filters (also used for the export). Empty values are omitted. */
export function filtersToParams(f: QueryFilters): URLSearchParams {
  const p = new URLSearchParams();
  if (f.code) p.set("code", f.code);
  p.set("dateField", f.dateField);
  if (f.from) p.set("from", f.from);
  if (f.to) p.set("to", f.to);
  if (f.statuses.length) p.set("status", [...f.statuses].sort().join(","));
  if (f.portals.length) p.set("portal", [...f.portals].sort().join(","));
  if (f.states.length) p.set("state", [...f.states].sort().join(","));
  if (f.attributionStatuses.length) p.set("attribution", [...f.attributionStatuses].sort().join(","));
  return p;
}

export function leadsQueryToParams(q: LeadsQuery): URLSearchParams {
  const p = filtersToParams(q);
  p.set("page", String(q.page));
  p.set("pageSize", String(q.pageSize));
  p.set("sort", q.sortKey);
  p.set("dir", q.sortDir);
  return p;
}

type Parsed<T> = { ok: true; value: T } | { ok: false; errors: string[] };

function list(v: string | null): string[] {
  return v ? v.split(",").map((s) => s.trim()).filter(Boolean) : [];
}

/** Server-side parse + validation of filter params. Never trusts the client. */
export function parseFilterParams(sp: URLSearchParams): Parsed<QueryFilters> {
  const errors: string[] = [];
  const rawCode = sp.get("code") ?? "";
  const code = checkCode(rawCode);
  if (!code.ok) errors.push(`code: ${code.error}`);
  const dateField = (sp.get("dateField") ?? "entry") as DateField;
  if (!DATE_FIELDS.includes(dateField)) errors.push("dateField must be entry, disbursement or report.");
  const from = sp.get("from") ?? "", to = sp.get("to") ?? "";
  if (from || to) {
    const e = validateRange(from, to);
    if (e) errors.push(`date range: ${e}`);
  }
  const statuses = list(sp.get("status"));
  const portals = list(sp.get("portal"));
  const states = list(sp.get("state"));
  const attributionStatuses = list(sp.get("attribution")) as AttributionStatus[];
  if (statuses.some((s) => !TOKEN.test(s))) errors.push("status contains an invalid value.");
  if (portals.some((s) => !TOKEN.test(s))) errors.push("portal contains an invalid value.");
  if (states.some((s) => !STATE_RE.test(s))) errors.push("state contains an invalid value.");
  if (attributionStatuses.some((s) => !ATTR.includes(s))) errors.push("attribution contains an invalid value.");
  if (errors.length) return { ok: false, errors };
  return { ok: true, value: { code: code.ok ? code.value : "", dateField, from, to, statuses, portals, states, attributionStatuses } };
}

export function parseLeadsParams(sp: URLSearchParams): Parsed<LeadsQuery> {
  const f = parseFilterParams(sp);
  const errors = f.ok ? [] : [...f.errors];
  const page = Number(sp.get("page") ?? "1");
  const pageSize = Number(sp.get("pageSize") ?? DEFAULT_PAGE_SIZE);
  const sortKey = (sp.get("sort") ?? "leadEntryDate") as SortKey;
  const sortDir = (sp.get("dir") ?? "desc") as SortDir;
  if (!Number.isInteger(page) || page < 1 || page > 1_000_000) errors.push("page must be a positive integer.");
  if (!(PAGE_SIZES as readonly number[]).includes(pageSize)) errors.push(`pageSize must be one of ${PAGE_SIZES.join(", ")}.`);
  if (!SORT_KEYS.includes(sortKey)) errors.push("sort is not a sortable column.");
  if (sortDir !== "asc" && sortDir !== "desc") errors.push("dir must be asc or desc.");
  if (errors.length || !f.ok) return { ok: false, errors };
  return { ok: true, value: { ...f.value, page, pageSize, sortKey, sortDir } };
}

// ---- browser URL state (shareable / reload-safe) ---------------------------------------------

export interface UrlState {
  filters: QueryFilters;
  preset: DatePreset;
  page: number;
  pageSize: number;
  sortKey: SortKey;
  sortDir: SortDir;
}

export function stateToUrl(s: UrlState): string {
  const p = filtersToParams(s.filters);
  if (s.preset !== "custom") { p.delete("from"); p.delete("to"); p.set("range", s.preset); }
  if (s.page !== 1) p.set("page", String(s.page));
  if (s.pageSize !== DEFAULT_PAGE_SIZE) p.set("pageSize", String(s.pageSize));
  if (s.sortKey !== "leadEntryDate" || s.sortDir !== "desc") { p.set("sort", s.sortKey); p.set("dir", s.sortDir); }
  return p.toString();
}

/** Invalid or missing parts fall back to defaults; this never throws. */
export function urlToState(search: string, today: string): UrlState {
  const sp = new URLSearchParams(search);
  const rangeParam = sp.get("range") as DatePreset | null;
  const preset: DatePreset = rangeParam && PRESETS.includes(rangeParam) && rangeParam !== "custom"
    ? rangeParam : sp.get("from") && sp.get("to") ? "custom" : (DEFAULT_PRESET as DatePreset);
  if (preset !== "custom") {
    const r = resolvePreset(preset, today);
    sp.set("from", r.from); sp.set("to", r.to);
  }
  const parsed = parseLeadsParams(sp);
  if (parsed.ok) {
    const { page, pageSize, sortKey, sortDir, ...filters } = parsed.value;
    return { filters, preset, page, pageSize, sortKey, sortDir };
  }
  return { filters: emptyFilters(today), preset: DEFAULT_PRESET as DatePreset, page: 1, pageSize: DEFAULT_PAGE_SIZE, sortKey: "leadEntryDate", sortDir: "desc" };
}

export function sameFilters(a: QueryFilters, b: QueryFilters): boolean {
  return filtersToParams(a).toString() === filtersToParams(b).toString();
}
