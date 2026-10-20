import { formatDate, formatDateTime } from "../lib/dates";
import type { Async } from "../client/useDashboard";
import type { Meta } from "../lib/types";
import { cx } from "./ui";

/** Shows only what the backend actually reports. Demo mode says so plainly; nothing implies a live database. */
export function Header({ meta }: { meta: Async<Meta> }) {
  let pill: { tone: string; text: string; title: string } | null = null;
  if (meta.status === "ready" && meta.data) {
    const m = meta.data;
    if (m.mode === "demo") pill = { tone: "bg-amber-50 text-amber-900 ring-amber-600/30", text: "Demo data — synthetic, not connected to the database", title: "Numbers shown are generated for UI development and are not production results." };
    else if (m.sync && (m.sync.dataThroughReportDate || m.sync.lastLoadAt)) {
      const parts = [m.sync.dataThroughReportDate && `Data through ${formatDate(m.sync.dataThroughReportDate)}`, m.sync.lastLoadAt && `last load ${formatDateTime(m.sync.lastLoadAt)}`].filter(Boolean);
      pill = { tone: "bg-slate-100 text-slate-700 ring-slate-400/30", text: parts.join(" · "), title: "Reported by the MIS backend." };
    }
  } else if (meta.status === "error") {
    pill = { tone: "bg-red-50 text-red-800 ring-red-600/20", text: "Status unavailable", title: meta.error?.message ?? "" };
  }
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-[1500px] flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-6">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">Lead MIS</h1>
          <p className="text-sm text-slate-600">Partner performance and disbursement analytics</p>
        </div>
        {pill && (
          <p title={pill.title} className={cx("max-w-full rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset", pill.tone)} data-testid="sync-status">
            {pill.text}
          </p>
        )}
      </div>
    </header>
  );
}
