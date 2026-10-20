import type { ReactNode } from "react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

const FOCUS = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600";

export function Button(props: {
  children: ReactNode; onClick?: () => void; variant?: "primary" | "secondary" | "ghost"; type?: "button" | "submit";
  disabled?: boolean; busy?: boolean; className?: string; "aria-label"?: string; "aria-expanded"?: boolean; "aria-haspopup"?: "menu"; title?: string; id?: string;
}) {
  const v = props.variant ?? "secondary";
  return (
    <button
      id={props.id} type={props.type ?? "button"} onClick={props.onClick} disabled={props.disabled || props.busy} title={props.title}
      aria-label={props["aria-label"]} aria-expanded={props["aria-expanded"]} aria-haspopup={props["aria-haspopup"]} aria-busy={props.busy || undefined}
      className={cx(
        "inline-flex h-9 items-center justify-center gap-2 whitespace-nowrap rounded-md px-3.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50", FOCUS,
        v === "primary" && "bg-blue-600 text-white hover:bg-blue-700",
        v === "secondary" && "border border-slate-300 bg-white text-slate-800 hover:bg-slate-50",
        v === "ghost" && "text-slate-700 hover:bg-slate-100",
        props.className,
      )}
    >
      {props.busy && <Spinner />}
      {props.children}
    </button>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg className={cx("h-4 w-4 animate-spin", className)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("animate-pulse rounded bg-slate-200/80", className)} aria-hidden="true" />;
}

export function Banner(props: { tone: "error" | "warning" | "info" | "success"; title?: string; children?: ReactNode; action?: ReactNode }) {
  const tone = {
    error: "border-red-200 bg-red-50 text-red-900", warning: "border-amber-200 bg-amber-50 text-amber-900",
    info: "border-slate-200 bg-slate-50 text-slate-800", success: "border-green-200 bg-green-50 text-green-900",
  }[props.tone];
  return (
    <div role={props.tone === "error" ? "alert" : "status"} className={cx("flex flex-wrap items-start justify-between gap-3 rounded-md border px-4 py-3 text-sm", tone)}>
      <div>
        {props.title && <p className="font-semibold">{props.title}</p>}
        {props.children && <div className={props.title ? "mt-0.5" : ""}>{props.children}</div>}
      </div>
      {props.action}
    </div>
  );
}

export function Icon({ name, className }: { name: "search" | "chevron-down" | "chevron-right" | "download" | "x" | "columns" | "sort" | "sort-asc" | "sort-desc" | "filter" | "info" | "check" | "alert"; className?: string }) {
  const p = { fill: "none", stroke: "currentColor", strokeWidth: 1.75, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  const paths: Record<string, ReactNode> = {
    search: <><circle cx="11" cy="11" r="7" {...p} /><path d="m20 20-3.5-3.5" {...p} /></>,
    "chevron-down": <path d="m6 9 6 6 6-6" {...p} />,
    "chevron-right": <path d="m9 6 6 6-6 6" {...p} />,
    download: <><path d="M12 4v12" {...p} /><path d="m7 11 5 5 5-5" {...p} /><path d="M5 20h14" {...p} /></>,
    x: <path d="M6 6l12 12M18 6 6 18" {...p} />,
    columns: <><rect x="3" y="4" width="18" height="16" rx="2" {...p} /><path d="M9 4v16M15 4v16" {...p} /></>,
    sort: <path d="m8 9 4-4 4 4M8 15l4 4 4-4" {...p} />,
    "sort-asc": <path d="m8 14 4-4 4 4" {...p} />,
    "sort-desc": <path d="m8 10 4 4 4-4" {...p} />,
    filter: <path d="M4 5h16l-6 8v6l-4-2v-4L4 5Z" {...p} />,
    info: <><circle cx="12" cy="12" r="9" {...p} /><path d="M12 11v5M12 8h.01" {...p} /></>,
    check: <path d="m5 12 5 5 9-10" {...p} />,
    alert: <><path d="M12 4 3 20h18L12 4Z" {...p} /><path d="M12 10v4M12 17h.01" {...p} /></>,
  };
  return <svg viewBox="0 0 24 24" className={cx("h-4 w-4 shrink-0", className)} aria-hidden="true">{paths[name]}</svg>;
}
