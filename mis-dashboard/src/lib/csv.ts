export type CsvValue = string | number | null | undefined;

/** CSV-injection guard: text cells that a spreadsheet could read as a formula get a leading apostrophe. */
function guard(s: string): string {
  return /^[=+\-@\t\r]/.test(s) ? "'" + s : s;
}

export function csvCell(v: CsvValue): string {
  if (v === null || v === undefined) return "";
  const s = typeof v === "number" ? (Number.isFinite(v) ? String(v) : "") : guard(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** UTF-8 with BOM (so Excel reads Unicode), CRLF line ends, RFC 4180 quoting. */
export function toCsv(headers: string[], rows: CsvValue[][]): string {
  const lines = [headers.map(csvCell).join(",")];
  for (const r of rows) lines.push(r.map(csvCell).join(","));
  return "﻿" + lines.join("\r\n") + "\r\n";
}
