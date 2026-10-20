"use client";
import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { ATTRIBUTION_STATUS_LABEL, DATE_FIELD_LABEL } from "../lib/constants";
import { formatDate, formatDateTime } from "../lib/dates";
import { formatInr } from "../lib/format";
import type { Async } from "../client/useDashboard";
import type { LeadRow, LeadsPage, Meta, SortDir, SortKey } from "../lib/types";
import { Banner, Button, Icon, Skeleton, cx } from "./ui";
import { StatusBadge } from "./StatusBadge";

interface Col { key: SortKey; label: string; align?: "right"; render: (r: LeadRow, ctx: { statusLabel: (c: string) => string }) => ReactNode }

const dash = <span className="text-slate-400" aria-label="Not reported">—</span>;
const money = (v: number | null) => (v === null ? dash : <span className="tabular-nums">{formatInr(v, { decimals: "auto" })}</span>);
const text = (v: string | null) => (v ? v : dash);

export const COLUMNS: Col[] = [
  { key: "externalLeadId", label: "Lead ID", render: (r) => <span className="font-mono text-xs">{r.externalLeadId}</span> },
  { key: "attributionCode", label: "Code", render: (r) => r.attributionCode
      ? <span className="inline-flex items-center gap-1.5"><span className="font-mono text-xs font-semibold">{r.attributionCode}</span>{r.attributionStatus === "unregistered_code" && <span className="rounded bg-amber-50 px-1 text-[10px] font-medium text-amber-800 ring-1 ring-inset ring-amber-600/20" title="Code is not registered to a partner">unreg.</span>}</span>
      : <span className="text-xs text-slate-500">{ATTRIBUTION_STATUS_LABEL[r.attributionStatus]}</span> },
  { key: "portalName", label: "Portal", render: (r) => r.portalName },
  { key: "status", label: "Status", render: (r, c) => <StatusBadge code={r.status} label={c.statusLabel(r.status)} /> },
  { key: "leadEntryDate", label: "Entry date", render: (r) => <span className="whitespace-nowrap tabular-nums">{formatDate(r.leadEntryDate)}</span> },
  { key: "disbursedDate", label: "Disbursed on", render: (r) => <span className="whitespace-nowrap tabular-nums">{formatDate(r.disbursedDate)}</span> },
  { key: "requestedLoanAmount", label: "Requested", align: "right", render: (r) => money(r.requestedLoanAmount) },
  { key: "approvedAmount", label: "Approved", align: "right", render: (r) => money(r.approvedAmount) },
  { key: "disbursedAmount", label: "Disbursed", align: "right", render: (r) => money(r.disbursedAmount) },
  { key: "payoutAmount", label: "Payout", align: "right", render: (r) => money(r.payoutAmount) },
  { key: "state", label: "State", render: (r) => text(r.state) },
  { key: "city", label: "City", render: (r) => text(r.city) },
];

const STORAGE_KEY = "mis.visibleColumns";
function loadVisible(): SortKey[] {
  try {
    const v = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null");
    if (Array.isArray(v)) { const ok = COLUMNS.map((c) => c.key).filter((k) => v.includes(k)); if (ok.length) return ok; }
  } catch { /* storage blocked or corrupt: use defaults */ }
  return COLUMNS.map((c) => c.key);
}

function ColumnMenu({ visible, onChange }: { visible: SortKey[]; onChange: (v: SortKey[]) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc); document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <Button variant="secondary" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-haspopup="menu" id="columns-btn"><Icon name="columns" />Columns</Button>
      {open && (
        <div role="group" aria-label="Visible columns" className="absolute right-0 z-20 mt-1 w-56 rounded-md border border-slate-200 bg-white p-2 shadow-lg" data-testid="column-menu">
          {COLUMNS.map((c) => (
            <label key={c.key} className="flex items-center gap-2 rounded px-2 py-1 text-sm text-slate-700 hover:bg-slate-50">
              <input type="checkbox" className="h-4 w-4 rounded border-slate-300 text-blue-600" checked={visible.includes(c.key)} disabled={visible.length === 1 && visible.includes(c.key)}
                onChange={() => onChange(COLUMNS.map((x) => x.key).filter((k) => (k === c.key ? !visible.includes(k) : visible.includes(k))))} />
              {c.label}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

function Detail({ r }: { r: LeadRow }) {
  const items: [string, ReactNode][] = [
    ["Status as reported", text(r.statusRaw)], ["Status reason", text(r.statusReason)],
    ["Partner", r.partnerName ?? (r.attributionCode ? "Not registered" : "—")], ["Attribution", ATTRIBUTION_STATUS_LABEL[r.attributionStatus]],
    ["Identifier namespace", r.idNamespace], ["Report date", formatDate(r.reportDate)],
    ["Employment type", text(r.employmentType)], ["Lead source", text(r.leadSource)], ["Last observed", formatDateTime(r.lastObservedAt)],
  ];
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-3 px-4 py-3 text-sm sm:grid-cols-2 lg:grid-cols-3" data-testid="lead-detail">
      {items.map(([k, v]) => (
        <div key={k}><dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{k}</dt><dd className="mt-0.5 break-words text-slate-900">{v}</dd></div>
      ))}
    </dl>
  );
}

export function LeadsTable(props: {
  leads: Async<LeadsPage>; meta: Meta | null; sortKey: SortKey; sortDir: SortDir;
  onSort: (k: SortKey) => void; onRetry: () => void; onReset: () => void; toolbarRight: ReactNode; footer: ReactNode; dateFieldLabel: string;
}) {
  const { leads } = props;
  const [visible, setVisible] = useState<SortKey[]>(() => COLUMNS.map((c) => c.key));
  const [expanded, setExpanded] = useState<string | null>(null);
  useEffect(() => setVisible(loadVisible()), []);
  useEffect(() => { setExpanded(null); }, [leads.data]);
  const setVis = (v: SortKey[]) => { setVisible(v); try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(v)); } catch { /* optional convenience */ } };
  const cols = COLUMNS.filter((c) => visible.includes(c.key));
  const statusLabel = (c: string) => props.meta?.statuses.find((s) => s.code === c)?.label ?? c.replace(/_/g, " ");
  const loadingFresh = leads.status !== "ready" && !leads.data;
  const refreshing = leads.status === "loading" && !!leads.data;

  return (
    <section aria-labelledby="leads-heading" className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
        <div>
          <h2 id="leads-heading" className="text-base font-semibold text-slate-900">Leads</h2>
          <p className="text-xs text-slate-500">Date basis: <b className="font-medium text-slate-700">{props.dateFieldLabel}</b></p>
        </div>
        <div className="flex items-center gap-2"><ColumnMenu visible={visible} onChange={setVis} />{props.toolbarRight}</div>
      </div>

      {leads.status === "error" ? (
        <div className="p-4"><Banner tone="error" title="Could not load leads" action={<Button onClick={props.onRetry}>Retry</Button>}>{leads.error?.message}</Banner></div>
      ) : leads.status === "ready" && leads.data && leads.data.total === 0 ? (
        <div className="px-6 py-14 text-center" data-testid="empty-state">
          <p className="text-base font-medium text-slate-900">No leads match these filters</p>
          <p className="mx-auto mt-1 max-w-md text-sm text-slate-600">Try a wider date range, a different date basis, or remove a status or portal filter. The date basis decides which date the range applies to.</p>
          <div className="mt-4"><Button onClick={props.onReset}>Reset filters</Button></div>
        </div>
      ) : (
        <div className={cx("overflow-x-auto", refreshing && "opacity-60 transition-opacity")} aria-busy={leads.status === "loading"} data-testid="table-wrap">
          <table className="min-w-full border-separate border-spacing-0 text-sm" data-testid="leads-table">
            <caption className="sr-only">Leads matching the applied filters</caption>
            <thead>
              <tr>
                {cols.map((c, i) => {
                  const active = props.sortKey === c.key;
                  return (
                    <th key={c.key} scope="col" aria-sort={active ? (props.sortDir === "asc" ? "ascending" : "descending") : "none"}
                      className={cx("sticky top-0 border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-600 whitespace-nowrap", c.align === "right" ? "text-right" : "text-left", i === 0 && "left-0 z-10")}>
                      <button type="button" onClick={() => props.onSort(c.key)} data-testid={`sort-${c.key}`}
                        className={cx("inline-flex items-center gap-1 rounded uppercase tracking-wide hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-blue-600", c.align === "right" && "flex-row-reverse")}>
                        {c.label}
                        <Icon name={active ? (props.sortDir === "asc" ? "sort-asc" : "sort-desc") : "sort"} className={cx("h-3.5 w-3.5", active ? "text-blue-700" : "text-slate-400")} />
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {loadingFresh
                ? Array.from({ length: 8 }, (_, i) => (
                    <tr key={i} data-testid="skeleton-row">{cols.map((c) => <td key={c.key} className="border-b border-slate-100 px-3 py-3"><Skeleton className="h-4 w-full max-w-[110px]" /></td>)}</tr>))
                : leads.data?.items.map((r) => {
                    const isOpen = expanded === r.leadId;
                    return (
                      <Fragment key={r.leadId}>
                        <tr className={cx("group hover:bg-slate-50", isOpen && "bg-slate-50")} data-testid="lead-row">
                          {cols.map((c, i) => (
                            <td key={c.key} className={cx("whitespace-nowrap border-b border-slate-100 px-3 py-2.5 align-middle text-slate-800", c.align === "right" && "text-right", i === 0 && "sticky left-0 z-[1] bg-white group-hover:bg-slate-50", i === 0 && isOpen && "bg-slate-50")}>
                              {i === 0 ? (
                                <span className="inline-flex items-center gap-1">
                                  <button type="button" aria-expanded={isOpen} aria-label={`${isOpen ? "Hide" : "Show"} details for lead ${r.externalLeadId}`} data-testid="row-toggle"
                                    onClick={() => setExpanded(isOpen ? null : r.leadId)} className="rounded p-0.5 text-slate-500 hover:bg-slate-200 focus-visible:outline-2 focus-visible:outline-blue-600">
                                    <Icon name={isOpen ? "chevron-down" : "chevron-right"} />
                                  </button>
                                  {c.render(r, { statusLabel })}
                                </span>
                              ) : c.render(r, { statusLabel })}
                            </td>
                          ))}
                        </tr>
                        {isOpen && <tr><td colSpan={cols.length} className="border-b border-slate-200 bg-slate-50/70"><Detail r={r} /></td></tr>}
                      </Fragment>
                    );
                  })}
            </tbody>
          </table>
        </div>
      )}
      {props.footer}
    </section>
  );
}
