"use client";
import { useId, useState } from "react";
import { ATTRIBUTION_STATUS_LABEL, DATE_FIELD_HELP, DATE_FIELD_LABEL, PRESET_LABEL, PRESET_ORDER } from "../lib/constants";
import { checkCode } from "../lib/attribution";
import type { Draft, DraftErrors, PartnerState } from "../client/useDashboard";
import type { AttributionStatus, DateField, Meta } from "../lib/types";
import { Button, Icon, cx } from "./ui";
import { StateSearch } from "./StateSearch";

const INPUT = "h-9 w-full rounded-md border bg-white px-3 text-sm text-slate-900 placeholder:text-slate-400 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-blue-600";

function toggle<T>(list: T[], v: T): T[] { return list.includes(v) ? list.filter((x) => x !== v) : [...list, v]; }

function PartnerHint({ draft, errors, partner }: { draft: Draft; errors: DraftErrors; partner: PartnerState }) {
  const check = checkCode(draft.code);
  const live = !check.ok && (draft.code.trim().length > 3 || /[^0-9A-Za-z]/.test(draft.code)) ? check.error : undefined;
  const error = errors.code ?? live;
  if (error) return <p id="code-msg" role="alert" className="mt-1 text-xs text-red-700">{error}</p>;
  let text = "Leave blank to include all partners.", tone = "text-slate-500";
  if (check.ok && check.value) {
    if (partner.status === "loading") text = "Looking up partner…";
    else if (partner.status === "ready") {
      const p = partner.data;
      if (p.state === "resolved") { text = `Partner: ${p.partnerName}`; tone = "text-green-800"; }
      else if (p.state === "unregistered") { text = "Code not registered to a partner yet — leads carrying it can still be listed."; tone = "text-amber-800"; }
      else { text = "No partner or leads known for this code."; tone = "text-amber-800"; }
    } else if (partner.status === "error") text = "Partner lookup unavailable — you can still search.";
    else text = "Press Apply to search this code.";
  }
  return <p id="code-msg" className={cx("mt-1 text-xs", tone)} data-testid="partner-hint" aria-live="polite">{text}</p>;
}

export function FilterPanel(props: {
  draft: Draft; errors: DraftErrors; meta: Meta | null; partner: PartnerState; dirty: boolean; busy: boolean;
  patch: (p: Partial<Draft>) => void; onApply: () => void; onReset: () => void;
}) {
  const { draft, errors, meta, partner } = props;
  const [open, setOpen] = useState(false);
  const uid = useId();
  const advancedCount = draft.statuses.length + draft.portals.length + draft.attributionStatuses.length;
  return (
    <form className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm" aria-label="Filters" noValidate
      onSubmit={(e) => { e.preventDefault(); props.onApply(); }}>
      <div className="grid gap-4 md:grid-cols-12">
        <div className="md:col-span-3">
          <label htmlFor={`${uid}-code`} className="mb-1 block text-sm font-medium text-slate-800">Attribution code</label>
          <div className="relative">
            <Icon name="search" className="pointer-events-none absolute left-2.5 top-2.5 text-slate-400" />
            <input id={`${uid}-code`} data-testid="code-input" type="text" inputMode="text" autoComplete="off" spellCheck={false} maxLength={8}
              placeholder="027 or 27P" value={draft.code} aria-describedby="code-msg" aria-invalid={!!errors.code || undefined}
              onChange={(e) => props.patch({ code: e.target.value.toUpperCase() })}
              className={cx(INPUT, "pl-8 font-mono uppercase tracking-wider placeholder:normal-case placeholder:tracking-normal", errors.code ? "border-red-400" : "border-slate-300")} />
          </div>
          <PartnerHint draft={draft} errors={errors} partner={partner} />
        </div>

        <div className="md:col-span-6">
          <span id={`${uid}-range`} className="mb-1 block text-sm font-medium text-slate-800">Date range</span>
          <div role="radiogroup" aria-labelledby={`${uid}-range`} className="flex flex-wrap gap-1.5">
            {PRESET_ORDER.map((p) => (
              <button key={p} type="button" role="radio" aria-checked={draft.preset === p} data-testid={`preset-${p}`}
                onClick={() => props.patch({ preset: p })}
                className={cx("h-9 rounded-md border px-3 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600",
                  draft.preset === p ? "border-blue-600 bg-blue-50 font-medium text-blue-800" : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50")}>
                {PRESET_LABEL[p]}
              </button>
            ))}
          </div>
          {draft.preset === "custom" && (
            <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
              <div>
                <label htmlFor={`${uid}-from`} className="mb-1 block text-xs font-medium text-slate-600">From</label>
                <input id={`${uid}-from`} data-testid="from-input" type="date" value={draft.from} max={draft.to || undefined} aria-describedby="range-msg"
                  aria-invalid={!!errors.range || undefined} onChange={(e) => props.patch({ from: e.target.value })}
                  className={cx(INPUT, errors.range ? "border-red-400" : "border-slate-300")} />
              </div>
              <div>
                <label htmlFor={`${uid}-to`} className="mb-1 block text-xs font-medium text-slate-600">To</label>
                <input id={`${uid}-to`} data-testid="to-input" type="date" value={draft.to} min={draft.from || undefined} aria-describedby="range-msg"
                  aria-invalid={!!errors.range || undefined} onChange={(e) => props.patch({ to: e.target.value })}
                  className={cx(INPUT, errors.range ? "border-red-400" : "border-slate-300")} />
              </div>
            </div>
          )}
          {errors.range && <p id="range-msg" role="alert" className="mt-1 text-xs text-red-700" data-testid="range-error">{errors.range}</p>}
        </div>

        <div className="md:col-span-3">
          <label htmlFor={`${uid}-basis`} className="mb-1 block text-sm font-medium text-slate-800">Date basis</label>
          <select id={`${uid}-basis`} data-testid="date-field" value={draft.dateField} onChange={(e) => props.patch({ dateField: e.target.value as DateField })}
            className={cx(INPUT, "border-slate-300")} aria-describedby={`${uid}-basis-help`}>
            {(Object.keys(DATE_FIELD_LABEL) as DateField[]).map((k) => <option key={k} value={k}>{DATE_FIELD_LABEL[k]}</option>)}
          </select>
          <p id={`${uid}-basis-help`} className="mt-1 text-xs text-slate-500">{DATE_FIELD_HELP[draft.dateField]}</p>
        </div>
      </div>

      <div className="mt-4 md:max-w-md">
        <StateSearch options={meta?.states ?? []} selected={draft.states} onChange={(states) => props.patch({ states })} />
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-3">
        <Button variant="ghost" onClick={() => setOpen((o) => !o)} aria-expanded={open} id={`${uid}-adv-btn`}>
          <Icon name={open ? "chevron-down" : "chevron-right"} />
          More filters{advancedCount > 0 && <span className="rounded-full bg-blue-100 px-1.5 text-xs font-semibold text-blue-800" data-testid="adv-count">{advancedCount}</span>}
        </Button>
        <div className="flex items-center gap-2">
          {props.dirty && <span className="text-xs text-amber-800" data-testid="dirty-hint">Filters changed — not applied yet</span>}
          <Button variant="ghost" onClick={props.onReset} id="reset-btn">Reset</Button>
          <Button variant="primary" type="submit" busy={props.busy} id="apply-btn">Apply filters</Button>
        </div>
      </div>

      {open && (
        <div className="mt-3 grid gap-5 border-t border-slate-100 pt-4 md:grid-cols-3" role="group" aria-labelledby={`${uid}-adv-btn`} data-testid="advanced">
          <fieldset>
            <legend className="mb-1.5 text-sm font-medium text-slate-800">Lead status</legend>
            <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
              {(meta?.statuses ?? []).map((s) => (
                <label key={s.code} className="flex items-center gap-2 text-sm text-slate-700">
                  <input type="checkbox" className="h-4 w-4 rounded border-slate-300 text-blue-600 focus-visible:outline-2 focus-visible:outline-blue-600"
                    checked={draft.statuses.includes(s.code)} onChange={() => props.patch({ statuses: toggle(draft.statuses, s.code) })} />
                  {s.label}
                </label>
              ))}
              {!meta && <p className="text-xs text-slate-500">Status list unavailable.</p>}
            </div>
          </fieldset>
          <fieldset>
            <legend className="mb-1.5 text-sm font-medium text-slate-800">Reporting portal</legend>
            <div className="grid max-h-44 grid-cols-1 gap-1 overflow-y-auto sm:grid-cols-2">
              {(meta?.portals ?? []).map((p) => (
                <label key={p.code} className="flex items-center gap-2 text-sm text-slate-700">
                  <input type="checkbox" className="h-4 w-4 rounded border-slate-300 text-blue-600 focus-visible:outline-2 focus-visible:outline-blue-600"
                    checked={draft.portals.includes(p.code)} onChange={() => props.patch({ portals: toggle(draft.portals, p.code) })} />
                  {p.name}
                </label>
              ))}
              {!meta && <p className="text-xs text-slate-500">Portal list unavailable.</p>}
            </div>
          </fieldset>
          <fieldset>
            <legend className="mb-1.5 text-sm font-medium text-slate-800">Attribution status</legend>
            <div className="grid grid-cols-1 gap-1">
              {(Object.keys(ATTRIBUTION_STATUS_LABEL) as AttributionStatus[]).map((k) => (
                <label key={k} className="flex items-center gap-2 text-sm text-slate-700">
                  <input type="checkbox" className="h-4 w-4 rounded border-slate-300 text-blue-600 focus-visible:outline-2 focus-visible:outline-blue-600"
                    checked={draft.attributionStatuses.includes(k)} onChange={() => props.patch({ attributionStatuses: toggle(draft.attributionStatuses, k) })} />
                  {ATTRIBUTION_STATUS_LABEL[k]}
                </label>
              ))}
            </div>
          </fieldset>
        </div>
      )}
    </form>
  );
}
