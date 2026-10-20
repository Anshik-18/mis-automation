import { CONVERSION_FORMULA } from "../lib/constants";
import { formatCount, formatInr, formatPct } from "../lib/format";
import type { Async } from "../client/useDashboard";
import type { Summary } from "../lib/types";
import { Banner, Button, Skeleton, cx } from "./ui";

interface Card { id: string; label: string; value: string; note?: string; tone?: "green" | "amber" | "red"; muted?: boolean }

function cards(s: Summary): Card[] {
  return [
    { id: "total", label: "Total leads", value: formatCount(s.totalLeads) },
    { id: "disbursed-count", label: "Disbursed count", value: formatCount(s.disbursedCount), tone: "green" },
    {
      id: "disbursed-amount", label: "Disbursed amount",
      value: s.disbursedAmount === null ? "Not reported" : formatInr(s.disbursedAmount), muted: s.disbursedAmount === null, tone: "green",
      note: s.disbursedMissingAmountCount > 0 ? `${formatCount(s.disbursedMissingAmountCount)} disbursed lead${s.disbursedMissingAmountCount === 1 ? " has" : "s have"} no amount reported` : undefined,
    },
    { id: "payout", label: "Total payout", value: s.payout === null ? "Not reported" : formatInr(s.payout), muted: s.payout === null },
    { id: "approved", label: "Approved leads", value: formatCount(s.approvedCount), tone: "amber", note: "Approved + disbursement pending" },
    { id: "rejected", label: "Rejected leads", value: formatCount(s.rejectedCount), tone: "red" },
    { id: "conversion", label: "Conversion rate", value: formatPct(s.conversionRatePct), muted: s.conversionRatePct === null, note: "Disbursed ÷ total leads" },
  ];
}

const BAR: Record<string, string> = { green: "bg-green-500", amber: "bg-amber-500", red: "bg-red-500" };

export function MetricCards({ summary, onRetry }: { summary: Async<Summary>; onRetry: () => void }) {
  if (summary.status === "error") {
    return <Banner tone="error" title="Could not load summary metrics" action={<Button onClick={onRetry}>Retry</Button>}>{summary.error?.message}</Banner>;
  }
  const loading = summary.status !== "ready" || !summary.data;
  const list = summary.data ? cards(summary.data) : null;
  return (
    <section aria-labelledby="metrics-heading" aria-busy={loading}>
      <h2 id="metrics-heading" className="sr-only">Summary metrics</h2>
      <dl className={cx("grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7", summary.status === "loading" && summary.data && "opacity-60")} data-testid="metrics">
        {(list ?? Array.from({ length: 7 }, () => null)).map((c, i) => (
          <div key={c?.id ?? i} className="relative overflow-hidden rounded-lg border border-slate-200 bg-white p-4 shadow-sm" data-testid={c ? `metric-${c.id}` : undefined}>
            {c?.tone && <span className={cx("absolute inset-y-0 left-0 w-1", BAR[c.tone])} aria-hidden="true" />}
            {c ? (
              <>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{c.label}</dt>
                <dd className={cx("mt-1.5 break-words text-xl font-semibold tabular-nums", c.muted ? "text-slate-400" : "text-slate-900")}>{c.value}</dd>
                {c.note && <p className="mt-1 text-xs leading-snug text-slate-500">{c.note}</p>}
              </>
            ) : (
              <><Skeleton className="h-3 w-20" /><Skeleton className="mt-3 h-6 w-24" /></>
            )}
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-slate-500">{CONVERSION_FORMULA} Metrics cover every lead matching the applied filters, not just the current page.</p>
    </section>
  );
}
