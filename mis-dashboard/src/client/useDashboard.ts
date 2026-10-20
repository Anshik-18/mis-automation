import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DEFAULT_PAGE_SIZE, DEFAULT_PRESET } from "../lib/constants";
import { checkCode } from "../lib/attribution";
import { resolvePreset, todayInTz, validateRange } from "../lib/dates";
import { emptyFilters, filtersToParams, sameFilters, stateToUrl, urlToState } from "../lib/filters";
import type {
  AttributionStatus, DateField, DatePreset, ExportFormat, LeadsPage, LeadsQuery, Meta, PartnerLookup, QueryFilters, SortDir, SortKey, Summary,
} from "../lib/types";
import { ApiError, createApiClient, type ApiClient } from "./api";
import { useDebounced } from "./useDebounced";

export interface Draft {
  code: string;
  preset: DatePreset;
  dateField: DateField;
  from: string;
  to: string;
  statuses: string[];
  portals: string[];
  states: string[];
  attributionStatuses: AttributionStatus[];
}
export interface Applied {
  filters: QueryFilters;
  preset: DatePreset;
  page: number;
  pageSize: number;
  sortKey: SortKey;
  sortDir: SortDir;
}
export interface DraftErrors { code?: string; range?: string }
export interface Async<T> { status: "idle" | "loading" | "ready" | "error"; data: T | null; error: ApiError | null; forKey: string }
export type ExportState =
  | { status: "idle" }
  | { status: "loading"; format: ExportFormat }
  | { status: "success"; format: ExportFormat; filename: string; rowCount: number | null; mismatch: boolean }
  | { status: "error"; format: ExportFormat; message: string };
export type PartnerState = { status: "idle" } | { status: "loading" } | { status: "ready"; data: PartnerLookup } | { status: "error" };

const idle = <T,>(): Async<T> => ({ status: "idle", data: null, error: null, forKey: "" });

export function draftFromApplied(a: Applied): Draft {
  const f = a.filters;
  return { code: f.code, preset: a.preset, dateField: f.dateField, from: f.from, to: f.to, statuses: f.statuses, portals: f.portals, states: f.states, attributionStatuses: f.attributionStatuses };
}

/** Validates a draft and returns the filters to apply (presets are resolved against today in the business timezone). */
export function filtersFromDraft(d: Draft, today: string): { ok: true; filters: QueryFilters } | { ok: false; errors: DraftErrors } {
  const errors: DraftErrors = {};
  const code = checkCode(d.code);
  if (!code.ok) errors.code = code.error;
  let from = d.from, to = d.to;
  if (d.preset === "custom") {
    const e = validateRange(d.from, d.to);
    if (e) errors.range = e;
  } else ({ from, to } = resolvePreset(d.preset, today));
  if (errors.code || errors.range) return { ok: false, errors };
  return { ok: true, filters: { code: code.value, dateField: d.dateField, from, to, statuses: d.statuses, portals: d.portals, states: d.states, attributionStatuses: d.attributionStatuses } };
}

export function useDashboard(client: ApiClient = defaultClient) {
  const [ready, setReady] = useState(false);
  const [applied, setApplied] = useState<Applied | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [errors, setErrors] = useState<DraftErrors>({});
  const [meta, setMeta] = useState<Async<Meta>>(idle());
  const [summary, setSummary] = useState<Async<Summary>>(idle());
  const [leads, setLeads] = useState<Async<LeadsPage>>(idle());
  const [exportState, setExportState] = useState<ExportState>({ status: "idle" });
  const [nonce, setNonce] = useState(0);                       // bumped by "Retry"
  const exportAbort = useRef<AbortController | null>(null);

  // ---- initial state from the URL (client-only, avoids hydration mismatch) ---------------------------------
  useEffect(() => {
    const s = urlToState(window.location.search, todayInTz());
    const a: Applied = { filters: s.filters, preset: s.preset, page: s.page, pageSize: s.pageSize, sortKey: s.sortKey, sortDir: s.sortDir };
    setApplied(a); setDraft(draftFromApplied(a)); setReady(true);
  }, []);

  // ---- metadata (statuses, portals, real sync info) --------------------------------------------------------
  useEffect(() => {
    const ctl = new AbortController();
    setMeta({ status: "loading", data: null, error: null, forKey: "" });
    client.meta(ctl.signal)
      .then((data) => !ctl.signal.aborted && setMeta({ status: "ready", data, error: null, forKey: "" }))
      .catch((e: ApiError) => e.kind !== "aborted" && setMeta({ status: "error", data: null, error: e, forKey: "" }));
    return () => ctl.abort();
  }, [client, nonce]);

  // ---- data: both streams are driven by the SAME applied filters ---------------------------------------------
  const filtersKey = useMemo(() => (applied ? filtersToParams(applied.filters).toString() : ""), [applied]);
  const leadsKey = applied ? `${filtersKey}|${applied.page}|${applied.pageSize}|${applied.sortKey}|${applied.sortDir}` : "";

  useEffect(() => {
    if (!applied) return;
    const ctl = new AbortController();
    setSummary((p) => ({ status: "loading", data: p.forKey === filtersKey ? p.data : null, error: null, forKey: filtersKey }));
    client.summary(applied.filters, ctl.signal)
      .then((data) => !ctl.signal.aborted && setSummary({ status: "ready", data, error: null, forKey: filtersKey }))
      .catch((e: ApiError) => e.kind !== "aborted" && !ctl.signal.aborted && setSummary({ status: "error", data: null, error: e, forKey: filtersKey }));
    return () => ctl.abort();                                // an outdated response can never replace a newer one
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, filtersKey, nonce]);

  useEffect(() => {
    if (!applied) return;
    const ctl = new AbortController();
    setLeads((p) => ({ status: "loading", data: p.forKey === filtersKey ? p.data : null, error: null, forKey: filtersKey }));
    const q: LeadsQuery = { ...applied.filters, page: applied.page, pageSize: applied.pageSize, sortKey: applied.sortKey, sortDir: applied.sortDir };
    client.leads(q, ctl.signal)
      .then((data) => !ctl.signal.aborted && setLeads({ status: "ready", data, error: null, forKey: filtersKey }))
      .catch((e: ApiError) => e.kind !== "aborted" && !ctl.signal.aborted && setLeads({ status: "error", data: null, error: e, forKey: filtersKey }));
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, leadsKey, nonce]);

  // ---- keep the address bar in sync (no reload) ------------------------------------------------------------
  useEffect(() => {
    if (!applied) return;
    const qs = stateToUrl({ filters: applied.filters, preset: applied.preset, page: applied.page, pageSize: applied.pageSize, sortKey: applied.sortKey, sortDir: applied.sortDir });
    try { window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname); } catch { /* sandboxed frames */ }
  }, [applied]);

  // ---- partner lookup (debounced while typing; separate lookup for the applied code) -------------------------
  const draftPartner = usePartnerLookup(client, draft?.code ?? "", 350);
  const appliedPartner = usePartnerLookup(client, applied?.filters.code ?? "", 0);

  // ---- actions ----------------------------------------------------------------------------------------------
  const patchDraft = useCallback((patch: Partial<Draft>) => {
    setDraft((d) => {
      if (!d) return d;
      const next = { ...d, ...patch };
      if (patch.preset && patch.preset !== "custom") Object.assign(next, resolvePreset(patch.preset, todayInTz()));
      return next;
    });
    setErrors((e) => ({ ...e, ...(patch.code !== undefined ? { code: undefined } : {}), ...(patch.from !== undefined || patch.to !== undefined || patch.preset ? { range: undefined } : {}) }));
  }, []);

  const apply = useCallback((): boolean => {
    if (!draft || !applied) return false;
    const r = filtersFromDraft(draft, todayInTz());
    if (!r.ok) { setErrors(r.errors); return false; }
    setErrors({});
    setApplied({ ...applied, filters: r.filters, preset: draft.preset, page: 1 });
    setDraft((d) => d && { ...d, code: r.filters.code, from: r.filters.from, to: r.filters.to });
    setExportState({ status: "idle" });
    return true;
  }, [draft, applied]);

  const reset = useCallback(() => {
    const a: Applied = { filters: emptyFilters(todayInTz()), preset: DEFAULT_PRESET, page: 1, pageSize: DEFAULT_PAGE_SIZE, sortKey: "leadEntryDate", sortDir: "desc" };
    setErrors({}); setApplied(a); setDraft(draftFromApplied(a)); setExportState({ status: "idle" });
  }, []);

  /** Removing a chip changes the applied filters directly (and the draft, so they stay in step). */
  const removeFilter = useCallback((kind: "code" | "status" | "portal" | "state" | "attribution", value?: string) => {
    setApplied((a) => {
      if (!a) return a;
      const f = { ...a.filters };
      if (kind === "code") f.code = "";
      if (kind === "status") f.statuses = f.statuses.filter((s) => s !== value);
      if (kind === "portal") f.portals = f.portals.filter((s) => s !== value);
      if (kind === "state") f.states = f.states.filter((s) => s !== value);
      if (kind === "attribution") f.attributionStatuses = f.attributionStatuses.filter((s) => s !== value);
      const next = { ...a, filters: f, page: 1 };
      setDraft(draftFromApplied(next));
      return next;
    });
    setExportState({ status: "idle" });
  }, []);

  const setPage = useCallback((page: number) => setApplied((a) => a && { ...a, page }), []);
  const setPageSize = useCallback((pageSize: number) => setApplied((a) => a && { ...a, pageSize, page: 1 }), []);
  const setSort = useCallback((key: SortKey) => setApplied((a) => a && {
    ...a, page: 1, sortKey: key, sortDir: a.sortKey === key ? (a.sortDir === "asc" ? "desc" : "asc") : "asc" }), []);
  const retry = useCallback(() => setNonce((n) => n + 1), []);

  const runExport = useCallback(async (format: ExportFormat) => {
    if (!applied) return;
    exportAbort.current?.abort();
    const ctl = new AbortController();
    exportAbort.current = ctl;
    setExportState({ status: "loading", format });
    const q: LeadsQuery = { ...applied.filters, page: 1, pageSize: applied.pageSize, sortKey: applied.sortKey, sortDir: applied.sortDir };
    try {
      const file = await client.exportFile(q, format, ctl.signal);
      if (ctl.signal.aborted) return;
      triggerDownload(file.blob, file.filename);
      const total = leads.data?.total ?? null;
      setExportState({ status: "success", format, filename: file.filename, rowCount: file.rowCount, mismatch: file.rowCount !== null && total !== null && file.rowCount !== total });
    } catch (e) {
      if (ctl.signal.aborted || (e instanceof ApiError && e.kind === "aborted")) return;
      setExportState({ status: "error", format, message: e instanceof ApiError ? e.message : "The export failed." });
    }
  }, [applied, client, leads.data]);

  useEffect(() => () => exportAbort.current?.abort(), []);

  const dirty = useMemo(() => {
    if (!draft || !applied) return false;
    const r = filtersFromDraft(draft, todayInTz());
    return !r.ok || !sameFilters(r.filters, applied.filters) || draft.preset !== applied.preset;
  }, [draft, applied]);

  return { ready, applied, draft, errors, dirty, meta, summary, leads, exportState, draftPartner, appliedPartner,
    patchDraft, apply, reset, removeFilter, setPage, setPageSize, setSort, retry, runExport };
}

export function usePartnerLookup(client: ApiClient, code: string, debounceMs: number): PartnerState {
  const check = checkCode(code);
  const value = useDebounced(check.ok && check.value ? check.value : "", debounceMs);
  const [state, setState] = useState<PartnerState>({ status: "idle" });
  useEffect(() => {
    if (!value) { setState({ status: "idle" }); return; }
    const ctl = new AbortController();
    setState({ status: "loading" });
    client.partner(value, ctl.signal)
      .then((data) => !ctl.signal.aborted && setState({ status: "ready", data }))
      .catch((e: ApiError) => e.kind !== "aborted" && !ctl.signal.aborted && setState({ status: "error" }));
    return () => ctl.abort();
  }, [client, value]);
  if (!(check.ok && check.value)) return { status: "idle" };
  return check.value !== value ? { status: "loading" } : state;     // typing: don't show the previous code's answer
}

export function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.rel = "noopener"; a.style.display = "none";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}

const defaultClient: ApiClient = createApiClient();
