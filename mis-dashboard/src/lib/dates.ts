import { BUSINESS_TZ } from "./constants";
import type { DatePreset } from "./types";

const ISO = /^(\d{4})-(\d{2})-(\d{2})$/;

export function isValidIsoDate(s: string): boolean {
  const m = ISO.exec(s);
  if (!m) return false;
  const y = +m[1], mo = +m[2], d = +m[3];
  const dt = new Date(Date.UTC(y, mo - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d && y >= 2000 && y <= 2100;
}

/** Today's calendar date in the business timezone (not the browser's), as YYYY-MM-DD. */
export function todayInTz(now: Date = new Date(), tz: string = BUSINESS_TZ): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(now);
  const get = (t: string) => parts.find((p) => p.type === t)!.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

export function addDays(iso: string, n: number): string {
  const m = ISO.exec(iso)!;
  const dt = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3] + n));
  return dt.toISOString().slice(0, 10);
}

function firstOfMonth(iso: string): string { return iso.slice(0, 8) + "01"; }

export function resolvePreset(preset: Exclude<DatePreset, "custom">, today: string): { from: string; to: string } {
  switch (preset) {
    case "today": return { from: today, to: today };
    case "yesterday": { const y = addDays(today, -1); return { from: y, to: y }; }
    case "last7": return { from: addDays(today, -6), to: today };
    case "thisMonth": return { from: firstOfMonth(today), to: today };
    case "prevMonth": {
      const lastOfPrev = addDays(firstOfMonth(today), -1);
      return { from: firstOfMonth(lastOfPrev), to: lastOfPrev };
    }
  }
}

/** Returns an error message, or null when the range is valid. */
export function validateRange(from: string, to: string): string | null {
  if (!from && !to) return "Choose both a From and a To date.";
  if (!from) return "Choose a From date.";
  if (!to) return "Choose a To date.";
  if (!isValidIsoDate(from)) return "From is not a valid date.";
  if (!isValidIsoDate(to)) return "To is not a valid date.";
  if (to < from) return "The To date cannot be earlier than the From date.";
  return null;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** '2026-10-19' -> '19 Oct 2026'. Pure string/calendar maths, so no timezone shifts. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso || !isValidIsoDate(iso)) return "—";
  return `${+iso.slice(8, 10)} ${MONTHS[+iso.slice(5, 7) - 1]} ${iso.slice(0, 4)}`;
}

export function formatDateTime(iso: string | null | undefined, tz: string = BUSINESS_TZ): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", { timeZone: tz, day: "numeric", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false }).format(d) + " IST";
}
