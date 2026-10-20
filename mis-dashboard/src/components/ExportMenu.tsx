"use client";
import { useEffect, useRef, useState } from "react";
import type { ExportState } from "../client/useDashboard";
import type { ExportFormat } from "../lib/types";
import { Banner, Button, Icon } from "./ui";

export function ExportMenu({ disabled, state, onExport }: { disabled: boolean; state: ExportState; onExport: (f: ExportFormat) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc); document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);
  const loading = state.status === "loading";
  const pick = (f: ExportFormat) => { setOpen(false); onExport(f); };
  return (
    <div className="relative" ref={ref}>
      <Button variant="secondary" onClick={() => setOpen((o) => !o)} disabled={disabled || loading} busy={loading} aria-haspopup="menu" aria-expanded={open} id="export-btn">
        {!loading && <Icon name="download" />}{loading ? `Preparing ${state.format.toUpperCase()}…` : "Export"}{!loading && <Icon name="chevron-down" />}
      </Button>
      {open && (
        <div role="menu" aria-labelledby="export-btn" className="absolute right-0 z-20 mt-1 w-64 rounded-md border border-slate-200 bg-white p-1 shadow-lg">
          <button role="menuitem" type="button" data-testid="export-csv" onClick={() => pick("csv")} className="block w-full rounded px-3 py-2 text-left text-sm hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-blue-600">
            <span className="font-medium text-slate-900">CSV</span><span className="block text-xs text-slate-500">All matching leads, one flat sheet</span>
          </button>
          <button role="menuitem" type="button" data-testid="export-xlsx" onClick={() => pick("xlsx")} className="block w-full rounded px-3 py-2 text-left text-sm hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-blue-600">
            <span className="font-medium text-slate-900">Excel (.xlsx)</span><span className="block text-xs text-slate-500">Summary sheet + Leads sheet</span>
          </button>
        </div>
      )}
    </div>
  );
}

/** Success is only ever shown after a file was actually received and handed to the browser. */
export function ExportStatus({ state, onRetry }: { state: ExportState; onRetry: (f: ExportFormat) => void }) {
  if (state.status === "success") {
    return (
      <div data-testid="export-success">
        <Banner tone={state.mismatch ? "warning" : "success"} title={`Downloaded ${state.filename}`}>
          {state.rowCount !== null ? `${state.rowCount.toLocaleString("en-IN")} lead${state.rowCount === 1 ? "" : "s"} exported.` : "File received."}
          {state.mismatch && " This differs from the table total — the underlying data may have changed since the table loaded."}
        </Banner>
      </div>
    );
  }
  if (state.status === "error") {
    return (
      <div data-testid="export-error">
        <Banner tone="error" title={`${state.format.toUpperCase()} export failed`} action={<Button onClick={() => onRetry(state.format)}>Try again</Button>}>{state.message}</Banner>
      </div>
    );
  }
  return null;
}
