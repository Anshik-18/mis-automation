import { PAGE_SIZES } from "../lib/constants";
import { formatCount } from "../lib/format";
import { Button } from "./ui";

export function Pagination(props: {
  page: number; pageSize: number; total: number; disabled?: boolean;
  onPage: (p: number) => void; onPageSize: (n: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(props.total / props.pageSize));
  const from = props.total === 0 ? 0 : (props.page - 1) * props.pageSize + 1;
  const to = Math.min(props.total, props.page * props.pageSize);
  const go = (p: number) => props.onPage(Math.min(pages, Math.max(1, p)));
  return (
    <nav aria-label="Pagination" className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 px-4 py-3 text-sm text-slate-700">
      <p data-testid="result-range" aria-live="polite">
        {props.total === 0 ? "No results" : <>Showing <b>{formatCount(from)}–{formatCount(to)}</b> of <b>{formatCount(props.total)}</b></>}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2">
          <span className="text-slate-600">Rows</span>
          <select className="h-9 rounded-md border border-slate-300 bg-white px-2 focus-visible:outline-2 focus-visible:outline-blue-600" value={props.pageSize}
            disabled={props.disabled} onChange={(e) => props.onPageSize(Number(e.target.value))}>
            {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <div className="flex items-center gap-1">
          <Button variant="secondary" aria-label="First page" disabled={props.disabled || props.page <= 1} onClick={() => go(1)}>«</Button>
          <Button variant="secondary" aria-label="Previous page" disabled={props.disabled || props.page <= 1} onClick={() => go(props.page - 1)}>Prev</Button>
          <span className="px-2 tabular-nums" data-testid="page-indicator">Page {formatCount(props.page)} of {formatCount(pages)}</span>
          <Button variant="secondary" aria-label="Next page" disabled={props.disabled || props.page >= pages} onClick={() => go(props.page + 1)}>Next</Button>
          <Button variant="secondary" aria-label="Last page" disabled={props.disabled || props.page >= pages} onClick={() => go(pages)}>»</Button>
        </div>
      </div>
    </nav>
  );
}
