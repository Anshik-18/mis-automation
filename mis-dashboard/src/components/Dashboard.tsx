"use client";
import { useMemo } from "react";
import { DATE_FIELD_LABEL } from "../lib/constants";
import { useDashboard } from "../client/useDashboard";
import { ActiveFilters } from "./ActiveFilters";
import { ExportMenu, ExportStatus } from "./ExportMenu";
import { FilterPanel } from "./FilterPanel";
import { Header } from "./Header";
import { LeadsTable } from "./LeadsTable";
import { MetricCards } from "./MetricCards";
import { Pagination } from "./Pagination";
import { Banner, Skeleton } from "./ui";

export function Dashboard() {
  const d = useDashboard();
  const meta = d.meta.data;
  const total = d.leads.data?.total ?? 0;
  const exportDisabled = d.leads.status !== "ready" || total === 0;
  const configError = useMemo(() => [d.meta, d.summary, d.leads].find((s) => s.error?.kind === "not_configured")?.error ?? null, [d.meta, d.summary, d.leads]);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <Header meta={d.meta} />
      <main className="mx-auto max-w-[1500px] space-y-5 px-4 py-5 sm:px-6">
        {!d.ready || !d.draft || !d.applied ? (
          <div className="space-y-4" aria-busy="true" aria-label="Loading"><Skeleton className="h-40 w-full" /><Skeleton className="h-24 w-full" /><Skeleton className="h-96 w-full" /></div>
        ) : (
          <>
            {configError && <Banner tone="error" title="The data source is not configured">{configError.message}</Banner>}
            <FilterPanel draft={d.draft} errors={d.errors} meta={meta} partner={d.draftPartner} dirty={d.dirty}
              busy={d.summary.status === "loading" || d.leads.status === "loading"} patch={d.patchDraft} onApply={d.apply} onReset={d.reset} />
            <ActiveFilters applied={d.applied} meta={meta} partner={d.appliedPartner} onRemove={d.removeFilter} />
            <MetricCards summary={d.summary} onRetry={d.retry} />
            <LeadsTable leads={d.leads} meta={meta} sortKey={d.applied.sortKey} sortDir={d.applied.sortDir} onSort={d.setSort}
              onRetry={d.retry} onReset={d.reset} dateFieldLabel={DATE_FIELD_LABEL[d.applied.filters.dateField]}
              toolbarRight={<ExportMenu disabled={exportDisabled} state={d.exportState} onExport={d.runExport} />}
              footer={
                <>
                  <div className="px-4 pb-3 empty:hidden"><ExportStatus state={d.exportState} onRetry={d.runExport} /></div>
                  {d.leads.data && d.leads.data.total > 0 && (
                    <Pagination page={d.applied.page} pageSize={d.applied.pageSize} total={d.leads.data.total} disabled={d.leads.status === "loading"}
                      onPage={d.setPage} onPageSize={d.setPageSize} />
                  )}
                </>
              } />
          </>
        )}
      </main>
    </div>
  );
}
