// Browser tests (Playwright + Chromium) against the REAL components, REAL API handlers and the synthetic demo service.
// Sandbox harness: bundles with esbuild (not Next.js). Usage: node tests/e2e/run.mjs
import { createRequire } from "node:module";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const PY = process.platform === "win32" ? "python" : "python3";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOCAL = path.resolve(HERE, "../../node_modules");
const TOOLS = process.env.NPM_TOOLS ?? (fs.existsSync(path.join(LOCAL, "playwright")) ? LOCAL : "/opt/npm-tools/node_modules");
const require = createRequire(TOOLS + "/");
const { chromium } = require("playwright");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const art = path.join(root, "tests/e2e/artifacts");
const PORT = process.env.PORT ?? "4173";
const BASE = `http://127.0.0.1:${PORT}`;
const NOW = new Date("2026-10-19T10:00:00Z");

const build = spawnSync("node", [path.join(root, "tests/e2e/build.mjs")], { encoding: "utf8" });
if (build.status !== 0) { console.error(build.stdout, build.stderr); process.exit(1); }
const server = spawn(process.execPath, [require.resolve("tsx/cli"), path.join(root, "tests/e2e/server.ts")], { env: { ...process.env, PORT }, stdio: ["ignore", "pipe", "inherit"] });
await new Promise((res, rej) => { server.stdout.on("data", (d) => String(d).includes("listening") && res()); server.on("exit", () => rej(new Error("server exited"))); });

const browser = await chromium.launch();
const results = [];
const shots = [];

async function ctl(q) { await fetch(`${BASE}/__ctl?${q}`); }
const fault = (f) => ctl("fault=" + encodeURIComponent(JSON.stringify(f)));
const apiLog = async () => (await (await fetch(`${BASE}/__log`)).json());

async function open(search = "", { width = 1440, height = 1000, acceptDownloads = true } = {}) {
  await ctl("reset");
  const context = await browser.newContext({ viewport: { width, height }, acceptDownloads });
  const page = await context.newPage();
  await page.clock.install({ time: NOW });                        // "today" = 19 Oct 2026 in IST, same instant as the server
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => m.type() === "error" && !/Failed to load resource/.test(m.text()) && errors.push(m.text()));
  await page.goto(`${BASE}/${search}`);
  return { page, context, errors };
}
const T = (page, id) => page.getByTestId(id);
// Waits until nothing is loading, twice in a row (a click's state change and its fetch are asynchronous).
const settle = async (page) => {
  for (let i = 0; i < 2; i++) {
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
    await page.waitForFunction(() => !document.querySelector("[aria-busy=true]"));
    await page.waitForTimeout(120);
  }
};
const totalOf = async (page) => Number((await T(page, "result-range").innerText()).match(/of\s+([\d,]+)/)?.[1].replace(/,/g, "") ?? 0);
const metric = async (page, id) => (await T(page, `metric-${id}`).locator("dd").innerText()).trim();
const num = (s) => Number(s.replace(/[^\d.]/g, ""));
const rowsText = (page, col) => page.locator("[data-testid=lead-row]").evaluateAll((trs, c) => trs.map((tr) => tr.children[c].innerText.trim()), col);
async function waitRows(page) { await page.waitForSelector("[data-testid=lead-row]"); await settle(page); }

async function test(name, fn) {
  const t0 = Date.now();
  try { await fn(); results.push({ name, ok: true }); console.log(`  ok   ${name} (${Date.now() - t0} ms)`); }
  catch (e) { results.push({ name, ok: false, err: e }); console.log(`  FAIL ${name}\n       ${String(e.message ?? e).split("\n").slice(0, 6).join("\n       ")}`); }
}
const withPage = (search, opts, fn) => async () => { const o = await open(search, opts); try { await fn(o); assert.deepEqual(o.errors, [], "browser console/page errors"); } finally { await o.context.close(); } };

// ======================================================================================================
await test("initial load: metrics, table and an honest demo status (no fake 'connected' claim)", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  assert.match(await T(page, "sync-status").innerText(), /Demo data.*not connected/i);
  assert.equal((await page.locator("[data-testid=lead-row]").count()), 25);
  for (const id of ["total", "disbursed-count", "disbursed-amount", "payout", "approved", "rejected", "conversion"]) assert.ok(await T(page, `metric-${id}`).isVisible(), id);
  assert.match(await T(page, "date-basis-chip").innerText(), /Lead Entry Date: 13 Oct 2026 – 19 Oct 2026 \(Last 7 days\)/);
  assert.match(await page.locator("h1").innerText(), /Lead MIS/);
  assert.match(await page.getByText("Partner performance and disbursement analytics").innerText(), /analytics/);
}));

await test("INR and count formatting use Indian grouping", withPage("?range=thisMonth", {}, async ({ page }) => {
  await waitRows(page);
  assert.match(await metric(page, "disbursed-amount"), /^₹\d{1,2}(,\d{2})*,\d{3}$/);
  assert.match(await metric(page, "total"), /^\d+(,\d{2})*(,\d{3})?$/);
}));

await test("attribution code: leading zero kept, lower-case normalised, partner name shown, results filtered, URL updated", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  await T(page, "code-input").fill("027");
  await page.waitForFunction(() => document.querySelector("[data-testid=partner-hint]")?.textContent?.includes("Demo Partner 027"));
  assert.equal(await T(page, "code-input").inputValue(), "027");
  await page.locator("#apply-btn").click(); await settle(page);
  const codes = new Set(await rowsText(page, 1));
  assert.deepEqual([...codes], ["027"]);
  assert.match(page.url(), /code=027/);
  assert.match(await T(page, "chip-code").innerText(), /Code 027 · Demo Partner 027/);
  await T(page, "code-input").fill("27p"); assert.equal(await T(page, "code-input").inputValue(), "27P");
  await page.locator("#apply-btn").click(); await settle(page);
  assert.deepEqual([...new Set(await rowsText(page, 1))], ["27P"]);
  const log = await apiLog();
  assert.ok(log.some((l) => l.startsWith("summary?") && l.includes("code=27P")) && log.some((l) => l.startsWith("leads?") && l.includes("code=27P")));
  assert.ok(!log.some((l) => /code=27(&|$)/.test(l)));                       // '27' (leading zero lost) is never sent
}));

await test("unregistered / unknown / invalid codes are handled gracefully and invalid input makes no API call", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  await T(page, "code-input").fill("zz9");
  await page.waitForFunction(() => /not registered/.test(document.querySelector("[data-testid=partner-hint]")?.textContent ?? ""));
  await T(page, "code-input").fill("QQQ");
  await page.waitForFunction(() => /No partner or leads known/.test(document.querySelector("[data-testid=partner-hint]")?.textContent ?? ""));
  const before = (await apiLog()).length;
  for (const [bad, msg] of [["12", /exactly 3/], ["02-", /letters A–Z and digits/], ["0270", /exactly 3|exactly/]]) {
    await T(page, "code-input").fill(bad);
    await page.locator("#apply-btn").click();
    assert.match(await page.locator("#code-msg").innerText(), msg, bad);
    assert.equal(await T(page, "code-input").getAttribute("aria-invalid"), "true");
  }
  const after = (await apiLog()).filter((l) => !l.startsWith("partners/"));
  assert.equal(after.length, before - (await apiLog()).slice(0, before).filter((l) => l.startsWith("partners/")).length);   // no summary/leads calls were made
  await T(page, "code-input").fill("");
  await page.locator("#apply-btn").click(); await settle(page);
  assert.equal(await page.locator("#code-msg").getAttribute("role"), null);
}));

await test("custom range: validation (end before start, missing dates), then applies; presets resolve in IST", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  await T(page, "preset-custom").click();
  await T(page, "from-input").fill("2026-10-10"); await T(page, "to-input").fill("2026-10-01");
  const before = (await apiLog()).filter((l) => l.startsWith("leads?")).length;
  await page.locator("#apply-btn").click();
  assert.match(await T(page, "range-error").innerText(), /cannot be earlier/);
  assert.equal((await apiLog()).filter((l) => l.startsWith("leads?")).length, before);
  await T(page, "from-input").fill(""); await page.locator("#apply-btn").click();
  assert.match(await T(page, "range-error").innerText(), /From/);
  await T(page, "from-input").fill("2026-10-01"); await T(page, "to-input").fill("2026-10-10");
  await page.locator("#apply-btn").click(); await settle(page);
  assert.match(await T(page, "date-basis-chip").innerText(), /1 Oct 2026 – 10 Oct 2026/);
  assert.match(page.url(), /from=2026-10-01/); assert.match(page.url(), /to=2026-10-10/);
  const dates = await rowsText(page, 4);
  assert.ok(dates.length > 0 && dates.every((d) => /\d+ Oct 2026/.test(d) && parseInt(d) <= 10));
  for (const [id, re] of [["today", /19 Oct 2026 – 19 Oct 2026 \(Today\)/], ["yesterday", /18 Oct 2026 – 18 Oct 2026/], ["thisMonth", /1 Oct 2026 – 19 Oct 2026/], ["prevMonth", /1 Sep 2026 – 30 Sep 2026/], ["last7", /13 Oct 2026 – 19 Oct 2026/]]) {
    await T(page, `preset-${id}`).click(); await page.locator("#apply-btn").click(); await settle(page);
    assert.match(await T(page, "date-basis-chip").innerText(), re, id);
  }
}));

await test("switching date basis (entry / disbursement / report) changes the result set and is labelled everywhere", withPage("?range=thisMonth", {}, async ({ page }) => {
  await waitRows(page);
  const counts = {};
  for (const [v, label] of [["entry", "Lead Entry Date"], ["disbursement", "Disbursement Date"], ["report", "Report Date"]]) {
    await T(page, "date-field").selectOption(v); await page.locator("#apply-btn").click(); await settle(page);
    counts[v] = await totalOf(page);
    assert.match(await T(page, "date-basis-chip").innerText(), new RegExp(label));
    assert.match(await page.locator("#leads-heading + p").innerText(), new RegExp(label));
    assert.ok((await apiLog()).some((l) => l.startsWith("leads?") && l.includes(`dateField=${v}`)));
    if (v === "disbursement") assert.ok((await rowsText(page, 5)).every((d) => d !== "—"));       // every row has a disbursement date
    assert.equal(num(await metric(page, "total")), counts[v]);
  }
  assert.equal(new Set(Object.values(counts)).size, 3, JSON.stringify(counts));
}));

await test("advanced filters: status + portal + attribution, active chips, removal, reset", withPage("?range=thisMonth", {}, async ({ page }) => {
  await waitRows(page);
  const all = await totalOf(page);
  await page.getByRole("button", { name: /More filters/ }).click();
  await page.getByLabel("Rejected", { exact: true }).check();
  await page.getByLabel("Demo Portal 03").check();
  assert.equal((await T(page, "adv-count").innerText()), "2");
  await page.locator("#apply-btn").click(); await settle(page);
  const n = await totalOf(page);
  assert.ok(n < all && n > 0);
  assert.ok((await rowsText(page, 3)).every((s) => s === "Rejected"));
  assert.ok((await rowsText(page, 2)).every((s) => s === "Demo Portal 03"));
  assert.equal(num(await metric(page, "total")), n);
  assert.equal(num(await metric(page, "rejected")), n);                      // metrics follow the same filters
  assert.equal(num(await metric(page, "disbursed-count")), 0);
  assert.match(page.url(), /status=rejected/); assert.match(page.url(), /portal=demo_03/);
  await page.getByRole("button", { name: "Remove filter Status: Rejected" }).click(); await settle(page);
  assert.ok(await totalOf(page) > n);
  await page.getByRole("button", { name: /More filters/ }).click().catch(() => {});
  await T(page, "code-input").fill("027"); assert.ok(await T(page, "dirty-hint").isVisible());       // draft differs from what is shown
  await page.locator("#reset-btn").click(); await settle(page);
  assert.equal(await T(page, "code-input").inputValue(), "");
  assert.match(await T(page, "date-basis-chip").innerText(), /\(Last 7 days\)/);
  assert.equal(await totalOf(page) > 0, true);
  assert.ok(!(await T(page, "dirty-hint").isVisible().catch(() => false)));
  assert.doesNotMatch(page.url(), /status=|portal=|code=/);
}));

await test("state search: type-ahead narrows the list, keyboard + click selection, tags, chip, URL, metrics and export follow", withPage("?range=thisMonth", {}, async ({ page }) => {
  await waitRows(page);
  const all = await totalOf(page);
  const box = T(page, "state-search");
  assert.ok(await box.isVisible());                                           // on the main toolbar, not hidden under "More filters"
  await box.fill("tam");
  await page.waitForSelector("[data-testid=state-option]");
  const opts = await T(page, "state-option").allInnerTexts();
  assert.deepEqual(opts, ["Tamil Nadu"]);                                     // substring, case-insensitive; other states filtered out
  await box.fill("A");
  await page.waitForFunction(() => document.querySelectorAll("[data-testid=state-option]").length > 1);
  await box.fill("zzz");
  await page.getByText("No matching state").waitFor();
  assert.match(await T(page, "state-listbox").innerText(), /No matching state/);
  await box.fill("tam"); await page.keyboard.press("ArrowDown"); await page.keyboard.press("Enter");     // keyboard selection; must not submit the form
  assert.ok(await page.getByRole("button", { name: "Remove state Tamil Nadu" }).isVisible());
  assert.equal(await box.inputValue(), "");
  assert.ok(!(await T(page, "date-basis-chip").innerText()).includes("Tamil"));          // not applied yet
  assert.ok(await T(page, "dirty-hint").isVisible());
  await page.locator("#apply-btn").click(); await settle(page);
  const n = await totalOf(page);
  assert.ok(n > 0 && n < all);
  assert.ok((await rowsText(page, 10)).every((s) => s === "Tamil Nadu"));
  assert.equal(num(await metric(page, "total")), n);
  assert.match(page.url(), /state=Tamil(\+|%20)Nadu/);
  assert.ok(await page.getByRole("button", { name: "Remove filter State: Tamil Nadu" }).isVisible());
  await box.fill("ker"); await T(page, "state-option").first().click();                  // mouse selection
  await page.locator("#apply-btn").click(); await settle(page);
  assert.deepEqual([...new Set(await rowsText(page, 10))].sort(), ["Kerala", "Tamil Nadu"]);
  const both = await totalOf(page);
  await box.fill("tam"); await page.getByText("No matching state").waitFor(); assert.equal(await T(page, "state-option").count(), 0);          // already-selected states are not offered again
  await box.fill("");
  await page.locator("#export-btn").click();
  const [dl] = await Promise.all([page.waitForEvent("download"), T(page, "export-csv").click()]);
  const file = path.join(os.tmpdir(), "e2e-state-" + dl.suggestedFilename()); await dl.saveAs(file);
  const py = spawnSync(PY, ["-c", `
import csv,json,sys
r=list(csv.reader(open(sys.argv[1],encoding='utf-8-sig',newline='')))
print(json.dumps({'n':len(r)-1,'states':sorted({x[16] for x in r[1:]})}))`, file], { encoding: "utf8" });
  const o = JSON.parse(py.stdout);
  assert.equal(o.n, both); assert.deepEqual(o.states, ["Kerala", "Tamil Nadu"]);
  await page.getByRole("button", { name: "Remove filter State: Kerala" }).click(); await settle(page);
  assert.deepEqual([...new Set(await rowsText(page, 10))], ["Tamil Nadu"]);
  await page.reload(); await waitRows(page);
  assert.deepEqual([...new Set(await rowsText(page, 10))], ["Tamil Nadu"]);                // restored from the URL
  assert.ok(await page.getByRole("button", { name: "Remove state Tamil Nadu" }).isVisible());
  await T(page, "state-search").focus(); await page.keyboard.press("Backspace");           // Backspace on empty input removes the last tag
  assert.equal(await page.getByRole("button", { name: "Remove state Tamil Nadu" }).count(), 0);
}));

await test("metrics always equal the table total for the same applied filters (many combinations)", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  const combos = [{ code: "027" }, { code: "000", range: "thisMonth" }, { code: "A1B", basis: "disbursement", range: "thisMonth" }, { code: "9ZK", basis: "report", range: "prevMonth" }, { code: "0A0", range: "yesterday" }, { code: "", range: "last7", basis: "entry" }];
  for (const c of combos) {
    await T(page, "code-input").fill(c.code);
    if (c.range) await T(page, `preset-${c.range}`).click();
    if (c.basis) await T(page, "date-field").selectOption(c.basis);
    await page.locator("#apply-btn").click(); await settle(page);
    const tot = await totalOf(page).catch(() => 0);
    const empty = await T(page, "empty-state").isVisible().catch(() => false);
    assert.equal(num(await metric(page, "total")), empty ? 0 : tot, JSON.stringify(c));
  }
}));

await test("empty state: helpful message, zeros distinct from 'not reported', export disabled", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  await T(page, "code-input").fill("ZZZ"); await page.locator("#apply-btn").click(); await settle(page);
  assert.ok(await T(page, "empty-state").isVisible());
  assert.match(await T(page, "empty-state").innerText(), /No leads match these filters/);
  assert.equal(await metric(page, "total"), "0");
  assert.equal(await metric(page, "conversion"), "—");
  assert.equal(await metric(page, "disbursed-amount"), "Not reported");
  assert.ok(await page.locator("#export-btn").isDisabled());
  await T(page, "empty-state").getByRole("button", { name: "Reset filters" }).click(); await waitRows(page);
}));

await test("loading state shows skeleton rows and metric placeholders, never stale numbers for new filters", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  await fault({ path: "leads", mode: "delay", ms: 900, match: "code=027" });
  await fault({ path: "summary", mode: "delay", ms: 900, match: "code=027" });
  await T(page, "code-input").fill("027"); await page.locator("#apply-btn").click();
  await page.waitForSelector("[data-testid=skeleton-row]");
  assert.equal(await page.locator("[data-testid=lead-row]").count(), 0);                  // previous filters' rows are not shown
  assert.equal(await page.locator("[data-testid^=metric-]").count(), 0);                  // ...nor their metrics
  assert.equal(await page.locator("[aria-busy=true]").first().isVisible(), true);
  await waitRows(page);
  assert.ok(await page.locator("[data-testid=lead-row]").count() > 0);
}));

await test("error states: table and metrics fail independently, retry recovers; bad shape and network drops are caught", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  await fault({ path: "leads", mode: "error", match: "code=027" });
  await fault({ path: "summary", mode: "badshape", match: "code=027" });
  await T(page, "code-input").fill("027"); await page.locator("#apply-btn").click();
  await page.getByText("Could not load leads").waitFor();
  await page.getByText("Could not load summary metrics").waitFor();
  assert.match(await page.getByText(/unexpected summary response/).innerText(), /unexpected/);
  assert.match(await page.getByText("Simulated server failure.").innerText(), /Simulated/);
  assert.ok(await page.locator("#export-btn").isDisabled());
  await ctl("reset");
  await page.getByRole("button", { name: "Retry" }).first().click();
  await waitRows(page);
  assert.equal((await metric(page, "total")).length > 0, true);
  await fault({ path: "leads", mode: "drop", match: "code=000" });
  await T(page, "code-input").fill("000"); await page.locator("#apply-btn").click();
  await page.getByText(/Could not reach the server/).waitFor();
}));

await test("pagination: pages cover results exactly once, page size and first/last work, sort resets to page 1", withPage("?range=thisMonth&dateField=entry", {}, async ({ page }) => {
  await waitRows(page);
  const total = await totalOf(page);
  const pages = Math.ceil(total / 25);
  assert.match(await T(page, "page-indicator").innerText(), new RegExp(`Page 1 of ${pages}`));
  const p1 = await rowsText(page, 0);
  await page.getByRole("button", { name: "Next page" }).click(); await settle(page);
  const p2 = await rowsText(page, 0);
  assert.match(await T(page, "page-indicator").innerText(), /Page 2/);
  assert.equal(p1.filter((x) => p2.includes(x)).length, 0);
  assert.match(page.url(), /page=2/);
  await page.getByRole("button", { name: "Last page" }).click(); await settle(page);
  assert.equal(await page.locator("[data-testid=lead-row]").count(), total - (pages - 1) * 25);
  assert.ok(await page.getByRole("button", { name: "Next page" }).isDisabled());
  await page.getByRole("button", { name: "First page" }).click(); await settle(page);
  assert.ok(await page.getByRole("button", { name: "Previous page" }).isDisabled());
  await page.getByLabel("Rows").selectOption("100"); await settle(page);
  assert.equal(await page.locator("[data-testid=lead-row]").count(), Math.min(100, total));
  await page.getByRole("button", { name: "Next page" }).click(); await settle(page);
  await T(page, "sort-city").click(); await settle(page);
  assert.match(await T(page, "page-indicator").innerText(), /Page 1 of/);
  assert.equal(await totalOf(page), total);
}));

await test("sorting: ascending/descending, aria-sort, numeric order, nulls last, URL kept", withPage("?range=thisMonth", {}, async ({ page }) => {
  await waitRows(page);
  await page.getByLabel("Rows").selectOption("100"); await settle(page);
  const th = (k) => page.locator("th", { has: T(page, `sort-${k}`) });
  await T(page, "sort-disbursedAmount").click(); await settle(page);
  assert.equal(await th("disbursedAmount").getAttribute("aria-sort"), "ascending");
  const asc = (await rowsText(page, 8)).map((s) => (s === "—" ? null : num(s)));
  const firstNull = asc.indexOf(null);
  const known = firstNull === -1 ? asc : asc.slice(0, firstNull);
  assert.deepEqual(known, [...known].sort((a, b) => a - b));
  assert.ok(firstNull === -1 || asc.slice(firstNull).every((v) => v === null));
  await T(page, "sort-disbursedAmount").click(); await settle(page);
  assert.equal(await th("disbursedAmount").getAttribute("aria-sort"), "descending");
  const desc = (await rowsText(page, 8)).filter((s) => s !== "—").map(num);
  assert.deepEqual(desc, [...desc].sort((a, b) => b - a));
  assert.match(page.url(), /sort=disbursedAmount/); assert.match(page.url(), /dir=desc/);
  await T(page, "sort-externalLeadId").click(); await settle(page);
  assert.equal(await th("disbursedAmount").getAttribute("aria-sort"), "none");
}));

await test("outdated responses never replace newer ones (slow request for A resolves after B)", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  await fault({ path: "leads", mode: "delay", ms: 1500, match: "code=027" });
  await fault({ path: "summary", mode: "delay", ms: 1500, match: "code=027" });
  await T(page, "code-input").fill("027"); await page.locator("#apply-btn").click();
  await page.waitForSelector("[data-testid=skeleton-row]");
  await T(page, "code-input").fill("27P"); await page.locator("#apply-btn").click();
  await waitRows(page);
  await page.waitForTimeout(1900);                                           // the slow 027 responses have now arrived (and were discarded)
  assert.deepEqual([...new Set(await rowsText(page, 1))], ["27P"]);
  assert.match(await T(page, "chip-code").innerText(), /27P/);
  assert.equal(num(await metric(page, "total")), await totalOf(page));
}));

await test("row details expand with keyboard-accessible toggle; column visibility persists across reloads", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  const t = page.locator("[data-testid=row-toggle]").first();
  assert.equal(await t.getAttribute("aria-expanded"), "false");
  await t.focus(); await page.keyboard.press("Enter");
  assert.equal(await t.getAttribute("aria-expanded"), "true");
  const d = (await T(page, "lead-detail").textContent()) ?? "";
  for (const k of ["Status as reported", "Partner", "Attribution", "Report date", "Employment type", "Lead source", "Last observed", "Identifier namespace"]) assert.ok(d.includes(k), k);
  await page.keyboard.press("Enter"); assert.equal(await T(page, "lead-detail").count(), 0);
  await page.locator("#columns-btn").click();
  await page.getByLabel("Payout", { exact: true }).uncheck(); await page.getByLabel("City", { exact: true }).uncheck();
  assert.equal(await page.locator("th").count(), 10);
  await page.reload(); await waitRows(page);
  assert.equal(await page.locator("th").count(), 10);
}));

await test("CSV export: downloads ALL matching rows (not one page), honours filters, escapes content", withPage("?code=027&range=thisMonth&dateField=entry", {}, async ({ page }) => {
  await waitRows(page);
  const total = await totalOf(page); assert.ok(total > 25);
  await page.locator("#export-btn").click();
  const [dl] = await Promise.all([page.waitForEvent("download"), T(page, "export-csv").click()]);
  const file = path.join(os.tmpdir(), "e2e-" + dl.suggestedFilename()); await dl.saveAs(file);
  assert.match(dl.suggestedFilename(), /^lead-mis_027_entry_2026-10-01_2026-10-19\.csv$/);
  const py = spawnSync(PY, ["-c", `
import csv,json,sys
r=list(csv.reader(open(sys.argv[1],encoding='utf-8-sig',newline='')))
print(json.dumps({'n':len(r)-1,'codes':sorted({x[2] for x in r[1:]}),'hdr':r[0][0],'w':sorted({len(x) for x in r}),'dates':[min(x[9] for x in r[1:]),max(x[9] for x in r[1:])]}))`, file], { encoding: "utf8" });
  const o = JSON.parse(py.stdout);
  assert.equal(o.n, total); assert.deepEqual(o.codes, ["027"]); assert.equal(o.hdr, "Lead ID"); assert.deepEqual(o.w, [21]);
  assert.ok(o.dates[0] >= "2026-10-01" && o.dates[1] <= "2026-10-19");
  await T(page, "export-success").waitFor();
  assert.match(await T(page, "export-success").innerText(), new RegExp(`${total} leads exported`));
}));

await test("Excel export: Summary + Leads sheets, date basis and filters preserved, row count equals table total", withPage("?dateField=disbursement&range=thisMonth&status=disbursed&attribution=resolved", {}, async ({ page }) => {
  await waitRows(page);
  const total = await totalOf(page); const disbCount = num(await metric(page, "disbursed-count"));
  await page.locator("#export-btn").click();
  const [dl] = await Promise.all([page.waitForEvent("download"), T(page, "export-xlsx").click()]);
  const file = path.join(os.tmpdir(), "e2e-" + dl.suggestedFilename()); await dl.saveAs(file);
  const py = spawnSync(PY, ["-c", `
import json,sys,openpyxl
wb=openpyxl.load_workbook(sys.argv[1]); s=wb['Summary']; l=wb['Leads']
kv={str(r[0].value):r[1].value for r in s.iter_rows() if r[0].value and len(r)>1}
rows=list(l.iter_rows(values_only=True))
print(json.dumps({'sheets':wb.sheetnames,'n':len(rows)-1,'basis':kv.get('Date basis'),'status':kv.get('Lead status'),'attr':kv.get('Attribution status'),'disb':kv.get('Total disbursed count'),'tot':kv.get('Total leads'),'rowsCell':kv.get('Rows in Leads sheet'),'dateCol':[r[10] is not None for r in rows[1:]]}, default=str))`, file], { encoding: "utf8" });
  const o = JSON.parse(py.stdout);
  assert.deepEqual(o.sheets, ["Summary", "Leads"]); assert.equal(o.n, total); assert.equal(o.tot, total); assert.equal(o.rowsCell, total);
  assert.equal(o.basis, "Disbursement Date"); assert.equal(o.status, "disbursed"); assert.equal(o.attr, "resolved"); assert.equal(o.disb, disbCount);
  assert.ok(o.dateCol.every(Boolean));
  assert.match(dl.suggestedFilename(), /\.xlsx$/);
}));

await test("export failure and empty-file responses show an error and never a success message", withPage("?range=thisMonth", {}, async ({ page }) => {
  await waitRows(page);
  await fault({ path: "export", mode: "error" });
  await page.locator("#export-btn").click(); await T(page, "export-csv").click();
  await T(page, "export-error").waitFor();
  assert.match(await T(page, "export-error").innerText(), /CSV export failed/);
  assert.equal(await T(page, "export-success").count(), 0);
  await ctl("reset"); await fault({ path: "export", mode: "empty" });
  await page.getByRole("button", { name: "Try again" }).click();
  await page.getByText(/empty file/).waitFor();
  assert.equal(await T(page, "export-success").count(), 0);
  await ctl("reset"); await fault({ path: "export", mode: "drop" });
  await page.getByRole("button", { name: "Try again" }).click();
  await page.getByText(/Could not reach the server/).waitFor();
  await ctl("reset");
  const [dl] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Try again" }).click()]);
  await T(page, "export-success").waitFor(); assert.ok(dl.suggestedFilename().endsWith(".csv"));
  assert.equal(await T(page, "export-error").count(), 0);
}));

await test("export button shows a busy state while the server prepares the file", withPage("?range=thisMonth", {}, async ({ page }) => {
  await waitRows(page);
  await fault({ path: "export", mode: "delay", ms: 1200 });
  await page.locator("#export-btn").click(); await T(page, "export-xlsx").click();
  await page.getByText("Preparing XLSX…").waitFor();
  assert.ok(await page.locator("#export-btn").isDisabled());
  await T(page, "export-success").waitFor({ timeout: 8000 });
}));

await test("URL state restores filters, date basis, sort and page on load", withPage("?code=27P&range=prevMonth&dateField=report&status=disbursed,rejected&sort=payoutAmount&dir=desc&pageSize=50", {}, async ({ page }) => {
  await page.waitForSelector("[data-testid=lead-row], [data-testid=empty-state]"); await settle(page);
  assert.equal(await T(page, "code-input").inputValue(), "27P");
  assert.equal(await T(page, "date-field").inputValue(), "report");
  assert.equal(await T(page, "preset-prevMonth").getAttribute("aria-checked"), "true");
  assert.match(await T(page, "date-basis-chip").innerText(), /Report Date: 1 Sep 2026 – 30 Sep 2026/);
  assert.equal(await page.getByLabel("Rows").inputValue().catch(() => "50"), "50");
  const log = await apiLog();
  assert.ok(log.some((l) => l.startsWith("leads?") && l.includes("sort=payoutAmount") && l.includes("status=disbursed%2Crejected")));
}));

await test("invalid URL parameters fall back to safe defaults", withPage("?code=zz&from=bad&page=-3&sort=drop%20table", {}, async ({ page }) => {
  await waitRows(page);
  assert.equal(await T(page, "code-input").inputValue(), "");
  assert.match(await T(page, "date-basis-chip").innerText(), /Last 7 days/);
}));

await test("keyboard: Enter in the code field applies; focus ring is visible; labels are programmatically associated", withPage("", {}, async ({ page }) => {
  await waitRows(page);
  await page.getByLabel("Attribution code").fill("000"); await page.keyboard.press("Enter");
  await T(page, "chip-code").waitFor(); await settle(page);
  assert.deepEqual([...new Set(await rowsText(page, 1))], ["000"]);
  await page.getByLabel("Date basis").focus(); await page.keyboard.press("Tab");
  await page.keyboard.press("Tab").catch(() => {});
  await page.locator("#apply-btn").focus(); await page.keyboard.press("Shift+Tab"); await page.keyboard.press("Tab");
  const outline = await page.locator("#apply-btn").evaluate((el) => getComputedStyle(el).outlineStyle + " " + getComputedStyle(el).outlineWidth);
  assert.match(outline, /solid 2px/);
  assert.ok(await page.getByRole("radiogroup").isVisible());
  assert.equal(await page.getByRole("radio", { name: "Last 7 days" }).getAttribute("aria-checked"), "true");
  assert.ok(await page.getByRole("table").getAttribute("data-testid") === "leads-table");
  const names = await page.locator("input:not([type=hidden]), select").evaluateAll((els) => els.filter((e) => !e.labels?.length && !e.getAttribute("aria-label")).map((e) => e.id || e.outerHTML.slice(0, 60)));
  assert.deepEqual(names, [], "form controls without an accessible label");
}));

for (const [w, h, name] of [[1440, 1000, "desktop"], [820, 1100, "tablet"], [390, 844, "mobile"]]) {
  await test(`responsive ${name} (${w}px): no page-level horizontal overflow, key actions reachable, table scrolls inside its card`, withPage("?range=thisMonth", { width: w, height: h }, async ({ page }) => {
    await waitRows(page);
    const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth,
      wrap: (() => { const e = document.querySelector("[data-testid=table-wrap]"); return { sw: e.scrollWidth, cw: e.clientWidth }; })() }));
    assert.ok(m.sw <= m.iw + 1, `page overflows: ${m.sw} > ${m.iw}`);
    if (w <= 820) assert.ok(m.wrap.sw > m.wrap.cw, "table should scroll horizontally inside its container");
    for (const sel of ["#apply-btn", "#reset-btn", "#export-btn", "[data-testid=code-input]", "[data-testid=date-field]"]) {
      const box = await page.locator(sel).boundingBox(); assert.ok(box && box.x >= 0 && box.x + box.width <= w + 1, `${sel} outside viewport`);
    }
    await page.getByRole("button", { name: /More filters/ }).click();
    const sw = await page.evaluate(() => document.documentElement.scrollWidth); assert.ok(sw <= w + 1);
    await T(page, "preset-custom").click();
    assert.ok((await page.evaluate(() => document.documentElement.scrollWidth)) <= w + 1);
    const f = path.join(art, `shot-${name}.png`); await page.screenshot({ path: f, fullPage: false }); shots.push(f);
    const metricsBox = await T(page, "metrics").boundingBox(); assert.ok(metricsBox.width <= w);
  }));
}

await test("no secrets or backend details reach the browser bundle", async () => {
  const js = fs.readFileSync(path.join(art, "bundle.js"), "utf8");
  for (const needle of ["MIS_API_KEY", "x-api-key", "MIS_API_BASE_URL", "postgres://"]) assert.ok(!js.includes(needle), needle);
});

// ======================================================================================================
await browser.close(); server.kill();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} browser tests passed` + (shots.length ? `; screenshots: ${shots.join(", ")}` : ""));
process.exit(failed.length ? 1 : 0);
