import { ATTRIBUTION_STATUS_LABEL, DATE_FIELD_LABEL, PRESET_LABEL } from "../lib/constants";
import { formatDate } from "../lib/dates";
import type { Applied, PartnerState } from "../client/useDashboard";
import type { Meta } from "../lib/types";
import { Icon } from "./ui";

function Chip({ label, onRemove, testId }: { label: string; onRemove?: () => void; testId?: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 py-0.5 pl-2.5 pr-1 text-xs font-medium text-slate-800" data-testid={testId}>
      {label}
      {onRemove && (
        <button type="button" onClick={onRemove} aria-label={`Remove filter ${label}`}
          className="rounded-full p-0.5 text-slate-500 hover:bg-slate-200 hover:text-slate-800 focus-visible:outline-2 focus-visible:outline-blue-600">
          <Icon name="x" className="h-3 w-3" />
        </button>
      )}
    </span>
  );
}

/** The single, always-visible statement of which filters the metrics and the table are showing. */
export function ActiveFilters(props: {
  applied: Applied; meta: Meta | null; partner: PartnerState; onRemove: (k: "code" | "status" | "portal" | "state" | "attribution", v?: string) => void;
}) {
  const f = props.applied.filters;
  const statusLabel = (c: string) => props.meta?.statuses.find((s) => s.code === c)?.label ?? c;
  const portalLabel = (c: string) => props.meta?.portals.find((p) => p.code === c)?.name ?? c;
  const partnerName = props.partner.status === "ready" ? props.partner.data.partnerName : null;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm" data-testid="active-filters" aria-label="Applied filters" role="group">
      <span className="font-medium text-slate-700">Results for</span>
      <span className="inline-flex items-center rounded-md bg-blue-50 px-2 py-0.5 text-xs font-semibold text-blue-800 ring-1 ring-inset ring-blue-600/20" data-testid="date-basis-chip">
        {DATE_FIELD_LABEL[f.dateField]}: {f.from ? `${formatDate(f.from)} – ${formatDate(f.to)}` : "any date"}{props.applied.preset !== "custom" && f.from ? ` (${PRESET_LABEL[props.applied.preset]})` : ""}
      </span>
      {f.code && <Chip testId="chip-code" label={`Code ${f.code}${partnerName ? ` · ${partnerName}` : ""}`} onRemove={() => props.onRemove("code")} />}
      {f.statuses.map((s) => <Chip key={s} label={`Status: ${statusLabel(s)}`} onRemove={() => props.onRemove("status", s)} />)}
      {f.portals.map((p) => <Chip key={p} label={`Portal: ${portalLabel(p)}`} onRemove={() => props.onRemove("portal", p)} />)}
      {f.states.map((s) => <Chip key={s} label={`State: ${s}`} onRemove={() => props.onRemove("state", s)} />)}
      {f.attributionStatuses.map((a) => <Chip key={a} label={`Attribution: ${ATTRIBUTION_STATUS_LABEL[a]}`} onRemove={() => props.onRemove("attribution", a)} />)}
      {!f.code && !f.statuses.length && !f.portals.length && !f.states.length && !f.attributionStatuses.length && <span className="text-xs text-slate-500">All partners, statuses, portals and states</span>}
    </div>
  );
}
