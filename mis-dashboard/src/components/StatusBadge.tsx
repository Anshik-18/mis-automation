import { cx } from "./ui";

const TONE: Record<string, string> = {
  disbursed: "bg-green-50 text-green-800 ring-green-600/20",
  approved: "bg-amber-50 text-amber-800 ring-amber-600/20",
  disbursement_pending: "bg-amber-50 text-amber-800 ring-amber-600/20",
  rejected: "bg-red-50 text-red-800 ring-red-600/20",
};
const NEUTRAL = "bg-slate-100 text-slate-700 ring-slate-500/20";

export function StatusBadge({ code, label }: { code: string; label?: string }) {
  const text = label ?? code.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  return (
    <span className={cx("inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset", TONE[code] ?? NEUTRAL)}>
      {text}
    </span>
  );
}
