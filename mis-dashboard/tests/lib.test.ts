import { test } from "node:test";
import assert from "node:assert/strict";
import { checkCode, normalizeCode } from "../src/lib/attribution";
import { addDays, formatDate, isValidIsoDate, resolvePreset, todayInTz, validateRange } from "../src/lib/dates";
import { formatCount, formatInr, formatPct } from "../src/lib/format";
import { filtersToParams, parseFilterParams, parseLeadsParams, stateToUrl, urlToState, emptyFilters } from "../src/lib/filters";
import { csvCell, toCsv } from "../src/lib/csv";

test("attribution codes keep leading zeros, normalise case, validate shape", () => {
  assert.deepEqual(checkCode("027"), { ok: true, value: "027" });
  assert.deepEqual(checkCode(" 27p "), { ok: true, value: "27P" });
  assert.deepEqual(checkCode("0a0"), { ok: true, value: "0A0" });
  assert.deepEqual(checkCode(""), { ok: true, value: "" });                       // empty = all partners
  assert.equal(normalizeCode("027"), "027");                                      // never coerced to the number 27
  for (const bad of ["27", "0270", "02-", "ÀBC", "Ａ27", "0 7"]) {
    const r = checkCode(bad);
    assert.equal(r.ok, false, bad);
  }
  assert.match((checkCode("27") as { error: string }).error, /exactly 3/);
  assert.match((checkCode("02-") as { error: string }).error, /letters A–Z and digits/);
});

test("date presets resolve in the business timezone", () => {
  // 2026-10-19 20:30 UTC is already 20 Oct 02:00 in India
  assert.equal(todayInTz(new Date("2026-10-19T20:30:00Z")), "2026-10-20");
  assert.equal(todayInTz(new Date("2026-10-19T10:00:00Z")), "2026-10-19");
  const today = "2026-10-20";
  assert.deepEqual(resolvePreset("today", today), { from: "2026-10-20", to: "2026-10-20" });
  assert.deepEqual(resolvePreset("yesterday", today), { from: "2026-10-19", to: "2026-10-19" });
  assert.deepEqual(resolvePreset("last7", today), { from: "2026-10-14", to: "2026-10-20" });
  assert.deepEqual(resolvePreset("thisMonth", today), { from: "2026-10-01", to: "2026-10-20" });
  assert.deepEqual(resolvePreset("prevMonth", today), { from: "2026-09-01", to: "2026-09-30" });
  assert.deepEqual(resolvePreset("prevMonth", "2026-01-15"), { from: "2025-12-01", to: "2025-12-31" });
  assert.deepEqual(resolvePreset("prevMonth", "2024-03-10"), { from: "2024-02-01", to: "2024-02-29" });   // leap year
  assert.deepEqual(resolvePreset("yesterday", "2026-03-01"), { from: "2026-02-28", to: "2026-02-28" });
  assert.equal(addDays("2026-12-31", 1), "2027-01-01");
});

test("custom range validation", () => {
  assert.equal(validateRange("2026-10-01", "2026-10-31"), null);
  assert.equal(validateRange("2026-10-01", "2026-10-01"), null);
  assert.match(validateRange("2026-10-05", "2026-10-01")!, /cannot be earlier/);
  assert.match(validateRange("", "2026-10-01")!, /From/);
  assert.match(validateRange("2026-10-01", "")!, /To/);
  assert.match(validateRange("", "")!, /both/);
  assert.match(validateRange("2026-02-30", "2026-03-01")!, /not a valid/);
  assert.equal(isValidIsoDate("2026-13-01"), false);
  assert.equal(formatDate("2026-10-09"), "9 Oct 2026");
  assert.equal(formatDate(null), "—");
});

test("INR uses Indian grouping and distinguishes missing from zero", () => {
  assert.equal(formatInr(139797), "₹1,39,797");
  assert.equal(formatInr(12345678), "₹1,23,45,678");
  assert.equal(formatInr(0), "₹0");
  assert.equal(formatInr(null), "—");
  assert.equal(formatInr(1250.5, { decimals: "auto" }), "₹1,250.50");
  assert.equal(formatInr(1250, { decimals: "auto" }), "₹1,250");
  assert.equal(formatCount(1234567), "12,34,567");
  assert.equal(formatCount(null), "—");
  assert.equal(formatPct(12.345), "12.3%");
  assert.equal(formatPct(null), "—");
});

test("filter params round-trip and are validated server-side", () => {
  const f = { code: "027", dateField: "disbursement" as const, from: "2026-10-01", to: "2026-10-19",
    statuses: ["rejected", "disbursed"], portals: ["demo_02"], states: ["Tamil Nadu", "Delhi"], attributionStatuses: ["resolved" as const] };
  const p = filtersToParams(f);
  assert.equal(p.get("status"), "disbursed,rejected");                            // canonical order => stable cache/keys
  const back = parseFilterParams(p);
  assert.ok(back.ok);
  assert.deepEqual(back.ok && back.value, { ...f, statuses: ["disbursed", "rejected"], states: ["Delhi", "Tamil Nadu"] });
  assert.equal(p.get("state"), "Delhi,Tamil Nadu");
  assert.ok(!parseFilterParams(new URLSearchParams("state=Delhi;DROP")).ok);
  assert.ok(!parseFilterParams(new URLSearchParams("state=%3Cscript%3E")).ok);
  const bad = parseFilterParams(new URLSearchParams("code=12&dateField=bogus&from=2026-10-05&to=2026-10-01&status=A%20B"));
  assert.ok(!bad.ok);
  assert.ok(!bad.ok && bad.errors.length >= 4);
  assert.ok(!parseLeadsParams(new URLSearchParams("page=0")).ok);
  assert.ok(!parseLeadsParams(new URLSearchParams("pageSize=7")).ok);
  assert.ok(!parseLeadsParams(new URLSearchParams("sort=password")).ok);
  assert.ok(parseLeadsParams(new URLSearchParams("sort=disbursedAmount&dir=asc&page=3&pageSize=50")).ok);
});

test("url state: presets are stored as presets, custom as dates, garbage falls back to defaults", () => {
  const today = "2026-10-20";
  const base = emptyFilters(today);
  const s = urlToState(stateToUrl({ filters: { ...base, code: "27P", dateField: "report" }, preset: "last7", page: 3, pageSize: 50, sortKey: "payoutAmount", sortDir: "asc" }), today);
  assert.equal(s.filters.code, "27P"); assert.equal(s.filters.dateField, "report"); assert.equal(s.preset, "last7");
  assert.equal(s.page, 3); assert.equal(s.pageSize, 50); assert.equal(s.sortKey, "payoutAmount"); assert.equal(s.sortDir, "asc");
  const custom = urlToState("from=2026-09-01&to=2026-09-15&code=027", today);
  assert.equal(custom.preset, "custom"); assert.equal(custom.filters.code, "027"); assert.equal(custom.filters.from, "2026-09-01");
  const junk = urlToState("code=zzzz&from=x&to=y&page=-4", today);
  assert.equal(junk.filters.code, ""); assert.equal(junk.page, 1);
  assert.ok(stateToUrl({ filters: base, preset: "last7", page: 1, pageSize: 25, sortKey: "leadEntryDate", sortDir: "desc" }).includes("range=last7"));
});

test("csv escapes commas, quotes, newlines, unicode, and neutralises formulas", () => {
  assert.equal(csvCell('a,b'), '"a,b"');
  assert.equal(csvCell('say "hi"'), '"say ""hi"""');
  assert.equal(csvCell("line1\nline2"), '"line1\nline2"');
  assert.equal(csvCell("पुणे"), "पुणे");
  assert.equal(csvCell("=SUM(1,1)"), `"'=SUM(1,1)"`);
  assert.equal(csvCell("+1"), "'+1");
  assert.equal(csvCell("@x"), "'@x");
  assert.equal(csvCell(-5), "-5");                                               // real numbers are untouched
  assert.equal(csvCell(null), "");
  const out = toCsv(["A", "B"], [["x", 1], ["y,z", null]]);
  assert.equal(out, "﻿A,B\r\nx,1\r\n\"y,z\",\r\n");
});
