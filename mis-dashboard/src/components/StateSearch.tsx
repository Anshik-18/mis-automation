"use client";
import { useId, useMemo, useRef, useState } from "react";
import { Icon, cx } from "./ui";

/** Type-ahead multi-select: type part of a state name, pick from the matches. Selected states show as removable tags. */
export function StateSearch(props: { options: string[]; selected: string[]; onChange: (next: string[]) => void; disabled?: boolean }) {
  const uid = useId();
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelClose = () => { if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null; } };

  const matches = useMemo(() => {
    const q = text.trim().toLowerCase();
    return props.options.filter((o) => !props.selected.includes(o) && (q === "" || o.toLowerCase().includes(q)))
      .sort((a, b) => Number(b.toLowerCase().startsWith(q)) - Number(a.toLowerCase().startsWith(q)) || a.localeCompare(b));
  }, [props.options, props.selected, text]);

  const add = (state: string) => { cancelClose(); props.onChange([...props.selected, state]); setText(""); setActive(0); setOpen(true); inputRef.current?.focus(); };
  const remove = (state: string) => props.onChange(props.selected.filter((s) => s !== state));
  const listId = `${uid}-list`;

  function onKeyDown(e: { key: string; preventDefault: () => void }) {
    if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActive((a) => Math.min(a + 1, Math.max(matches.length - 1, 0))); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
    else if (e.key === "Escape") { setOpen(false); }
    else if (e.key === "Enter") {
      if (open && text.trim() !== "" && matches.length > 0) { e.preventDefault(); add(matches[Math.min(active, matches.length - 1)]); }
      else if (text.trim() !== "") e.preventDefault();               // never submit the form with an unfinished state search
    } else if (e.key === "Backspace" && text === "" && props.selected.length) remove(props.selected[props.selected.length - 1]);
  }

  return (
    <div>
      <label htmlFor={`${uid}-in`} className="mb-1 block text-sm font-medium text-slate-800">State</label>
      <div className="relative">
        <Icon name="search" className="pointer-events-none absolute left-2.5 top-2.5 text-slate-400" />
        <input ref={inputRef} id={`${uid}-in`} data-testid="state-search" type="text" role="combobox" autoComplete="off" spellCheck={false}
          aria-expanded={open} aria-controls={listId} aria-autocomplete="list" aria-activedescendant={open && matches.length ? `${uid}-opt-${Math.min(active, matches.length - 1)}` : undefined}
          placeholder="Search state…" value={text} disabled={props.disabled}
          onChange={(e) => { cancelClose(); setText(e.target.value); setActive(0); setOpen(true); }}
          onFocus={() => { cancelClose(); setOpen(true); }} onBlur={() => { cancelClose(); closeTimer.current = setTimeout(() => setOpen(false), 120); }} onKeyDown={onKeyDown}
          className="h-9 w-full rounded-md border border-slate-300 bg-white pl-8 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-blue-600" />
        {open && (
          <ul id={listId} role="listbox" aria-label="Matching states" data-testid="state-listbox"
            className="absolute z-30 mt-1 max-h-60 w-full overflow-y-auto rounded-md border border-slate-200 bg-white py-1 shadow-lg">
            {matches.length === 0 ? (
              <li role="option" aria-selected="false" aria-disabled="true" className="px-3 py-2 text-sm text-slate-500">
                {props.options.length === 0 ? "No states in the data yet" : text.trim() ? "No matching state" : "All states selected"}
              </li>
            ) : matches.map((o, i) => (
              <li key={o} id={`${uid}-opt-${i}`} role="option" aria-selected={i === active} data-testid="state-option"
                onMouseDown={(e) => { e.preventDefault(); add(o); }} onMouseEnter={() => setActive(i)}
                className={cx("cursor-pointer px-3 py-1.5 text-sm", i === active ? "bg-blue-50 text-blue-900" : "text-slate-800")}>
                {o}
              </li>
            ))}
          </ul>
        )}
      </div>
      {props.selected.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-1.5" aria-label="Selected states" data-testid="selected-states">
          {props.selected.map((s) => (
            <li key={s} className="inline-flex items-center gap-1 rounded-full bg-blue-50 py-0.5 pl-2.5 pr-1 text-xs font-medium text-blue-900 ring-1 ring-inset ring-blue-600/20">
              {s}
              <button type="button" aria-label={`Remove state ${s}`} onClick={() => remove(s)}
                className="rounded-full p-0.5 text-blue-700 hover:bg-blue-100 focus-visible:outline-2 focus-visible:outline-blue-600"><Icon name="x" className="h-3 w-3" /></button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
