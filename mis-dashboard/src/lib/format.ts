const inr0 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const inr2 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = new Intl.NumberFormat("en-IN");

export const NOT_REPORTED = "—";

/** ₹1,39,797. `null` (not reported) is shown as a dash, distinct from a real ₹0. */
export function formatInr(v: number | null | undefined, opts: { decimals?: "auto" | 0 | 2 } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return NOT_REPORTED;
  const mode = opts.decimals ?? 0;
  if (mode === 2 || (mode === "auto" && !Number.isInteger(v))) return inr2.format(v);
  return inr0.format(v);
}

export function formatCount(v: number | null | undefined): string {
  return v === null || v === undefined || Number.isNaN(v) ? NOT_REPORTED : num.format(v);
}

export function formatPct(v: number | null | undefined): string {
  return v === null || v === undefined || Number.isNaN(v) ? NOT_REPORTED : `${v.toFixed(1)}%`;
}
