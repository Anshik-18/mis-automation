import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { spawnSync as spawn } from "node:child_process";
const PY = process.platform === "win32" ? "python" : "python3";           // Python + openpyxl are used only to verify exports independently
const spawnSync = (_cmd: string, args: string[], o: object) => spawn(PY, args, o as never);
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createDemoService } from "../src/server/demoService";
import { handleMisRequest } from "../src/server/handlers";
import { readConfig } from "../src/server/config";
import { emptyFilters, filtersToParams, leadsQueryToParams } from "../src/lib/filters";
import type { LeadsPage, LeadsQuery, QueryFilters, Summary } from "../src/lib/types";

const NOW = new Date("2026-10-19T10:00:00Z");           // 15:30 IST, 19 Oct 2026
const TODAY = "2026-10-19";
const svc = createDemoService({ now: () => NOW });
const base: QueryFilters = { ...emptyFilters(TODAY), from: "2026-08-01", to: TODAY };
const lq = (f: Partial<LeadsQuery> = {}): LeadsQuery => ({ ...base, page: 1, pageSize: 25, sortKey: "leadEntryDate", sortDir: "desc", ...f });
const req = (path: string, params?: URLSearchParams, method = "GET") =>
  new Request(`http://app.test/api/mis/${path}${params ? "?" + params : ""}`, { method });
const call = async (path: string, params?: URLSearchParams) => {
  const r = await handleMisRequest(req(path, params), { config: { source: "demo" }, service: svc });
  return r;
};

test("every filter narrows the dataset and metrics are computed from the FULL filtered set", async () => {
  const all = await svc.leads(lq({ pageSize: 100 }));
  const s = await svc.summary(base);
  assert.equal(s.totalLeads, all.total);                                          // summary total == table total, same filters
  assert.ok(all.total > 1000);

  const byCode = await svc.leads(lq({ code: "027" }));
  assert.ok(byCode.total > 0 && byCode.total < all.total);
  assert.ok(byCode.items.every((r) => r.attributionCode === "027"));              // leading zero preserved
  assert.equal((await svc.summary({ ...base, code: "027" })).totalLeads, byCode.total);
  assert.equal((await svc.leads(lq({ code: "27P" }))).items.every((r) => r.attributionCode === "27P"), true);
  assert.equal((await svc.leads(lq({ code: "27" as string }))).total, 0);          // '27' is not '027'

  const one = await svc.leads(lq({ statuses: ["rejected"], portals: ["demo_03"] }));
  assert.ok(one.items.every((r) => r.status === "rejected" && r.portalCode === "demo_03"));
  const unreg = await svc.leads(lq({ attributionStatuses: ["unregistered_code"] }));
  assert.ok(unreg.total > 0 && unreg.items.every((r) => r.attributionCode === "ZZ9" && r.partnerName === null));
});

test("state filter: exact-state match, multi-select, consistent metrics, listed in meta, invalid values rejected", async () => {
  const meta = await svc.meta();
  assert.ok(meta.states.length >= 4 && meta.states.includes("Tamil Nadu"));
  const one = await svc.leads(lq({ states: ["Tamil Nadu"], pageSize: 100 }));
  assert.ok(one.total > 0 && one.items.every((r) => r.state === "Tamil Nadu"));
  const two = await svc.leads(lq({ states: ["Tamil Nadu", "Kerala"], pageSize: 100 }));
  const kerala = await svc.leads(lq({ states: ["Kerala"], pageSize: 100 }));
  assert.equal(two.total, one.total + kerala.total);
  assert.equal((await svc.summary({ ...base, states: ["Tamil Nadu"] })).totalLeads, one.total);
  const combo = await svc.leads(lq({ states: ["Tamil Nadu"], code: "027", statuses: ["rejected"], pageSize: 100 }));
  assert.ok(combo.items.every((r) => r.state === "Tamil Nadu" && r.attributionCode === "027" && r.status === "rejected"));
  assert.equal((await svc.leads(lq({ states: ["Atlantis"] }))).total, 0);
  const bad = await call("leads", new URLSearchParams("state=%3Cscript%3E"));
  assert.equal(bad.status, 400);
  const viaHttp = await (await call("leads", leadsQueryToParams(lq({ states: ["Tamil Nadu"] })))).json();
  assert.equal(viaHttp.total, one.total);
});

test("date basis is explicit: entry, disbursement and report dates give different, correct sets", async () => {
  const win = { from: "2026-10-01", to: "2026-10-19" };
  const e = await svc.leads(lq({ ...win, dateField: "entry", pageSize: 100 }));
  const d = await svc.leads(lq({ ...win, dateField: "disbursement", pageSize: 100 }));
  const r = await svc.leads(lq({ ...win, dateField: "report", pageSize: 100 }));
  assert.ok(e.items.every((x) => x.leadEntryDate! >= win.from && x.leadEntryDate! <= win.to));
  assert.ok(d.items.every((x) => x.disbursedDate! >= win.from && x.disbursedDate! <= win.to));     // never matches a lead without a disbursement date
  assert.ok(r.items.every((x) => x.reportDate! >= win.from && x.reportDate! <= win.to));
  assert.notEqual(e.total, d.total);
  assert.notEqual(e.total, r.total);
  const noDisb = await svc.leads(lq({ ...win, dateField: "disbursement", pageSize: 100 }));
  assert.ok(noDisb.items.every((x) => x.disbursedDate !== null));
});

test("metrics definitions: separate count vs amount, null != 0, no disbursement inferred from an amount", async () => {
  const s = await svc.summary(base);
  const full = (await svc.leads(lq({ pageSize: 100 })));
  assert.ok(s.disbursedCount > 0);
  assert.ok(s.disbursedAmount! > 0);
  assert.ok(s.disbursedMissingAmountCount > 0);                                   // some disbursed leads have no amount
  assert.equal(s.conversionRatePct, Math.round((s.disbursedCount / s.totalLeads) * 10000) / 100);
  // 'closed' leads carry a disbursed amount in the demo data but are NOT counted as disbursed
  const closed = await svc.summary({ ...base, statuses: ["closed"] });
  assert.equal(closed.disbursedCount, 0);
  assert.equal(closed.disbursedAmount, null);                                     // no disbursed lead => "not reported", not zero
  const empty = await svc.summary({ ...base, code: "ZZZ" });
  assert.deepEqual([empty.totalLeads, empty.conversionRatePct, empty.payout], [0, null, null]);
  assert.ok(full.total === s.totalLeads);
  // independent recomputation straight from the generated rows (not through the service's filter path)
  const raw = (await Promise.all(Array.from({ length: Math.ceil(full.total / 100) }, (_, i) => svc.leads(lq({ pageSize: 100, page: i + 1 }))))).flatMap((p) => p.items);
  const ind = raw.filter((r) => r.status === "disbursed");
  assert.equal(s.disbursedCount, ind.length);
  assert.equal(s.disbursedAmount, Math.round(ind.reduce((a, r) => a + (r.disbursedAmount ?? 0), 0) * 100) / 100);
  assert.equal(s.disbursedMissingAmountCount, ind.filter((r) => r.disbursedAmount === null).length);
  assert.equal(s.approvedCount, raw.filter((r) => r.status === "approved" || r.status === "disbursement_pending").length);
  assert.equal(s.rejectedCount, raw.filter((r) => r.status === "rejected").length);
});

test("pagination covers the set exactly once and sorting is correct and stable", async () => {
  const f = { from: "2026-09-01", to: TODAY };
  const first = await svc.leads(lq({ ...f, pageSize: 100, sortKey: "externalLeadId", sortDir: "asc" }));
  const ids: string[] = [];
  const pages = Math.ceil(first.total / 100);
  for (let p = 1; p <= pages; p++) ids.push(...(await svc.leads(lq({ ...f, pageSize: 100, page: p, sortKey: "externalLeadId", sortDir: "asc" }))).items.map((r) => r.leadId));
  assert.equal(ids.length, first.total);
  assert.equal(new Set(ids).size, first.total);                                   // no duplicates or gaps across pages
  const beyond = await svc.leads(lq({ ...f, pageSize: 100, page: pages + 5 }));
  assert.deepEqual([beyond.items.length, beyond.total], [0, first.total]);

  const asc = (await svc.leads(lq({ sortKey: "disbursedAmount", sortDir: "asc", pageSize: 100 }))).items.map((r) => r.disbursedAmount);
  const nonNull = asc.filter((v): v is number => v !== null);
  assert.deepEqual(nonNull, [...nonNull].sort((a, b) => a - b));
  const firstNull = asc.indexOf(null);
  assert.ok(firstNull === -1 || asc.slice(firstNull).every((v) => v === null));   // nulls last
  const desc = (await svc.leads(lq({ sortKey: "disbursedAmount", sortDir: "desc", pageSize: 100 }))).items.map((r) => r.disbursedAmount);
  const nn = desc.filter((v): v is number => v !== null);
  assert.deepEqual(nn, [...nn].sort((a, b) => b - a));
  const a = await svc.leads(lq({ sortKey: "status", pageSize: 50 })), b = await svc.leads(lq({ sortKey: "status", pageSize: 50 }));
  assert.deepEqual(a.items.map((r) => r.leadId), b.items.map((r) => r.leadId));  // deterministic tie-break
});

test("HTTP handlers: shapes, validation errors, method and path guards", async () => {
  const ok = await call("leads", leadsQueryToParams(lq({ code: "027" })));
  assert.equal(ok.status, 200);
  assert.equal(ok.headers.get("cache-control"), "no-store");
  const page = (await ok.json()) as LeadsPage;
  assert.ok(page.total > 0 && page.items.length <= 25);
  const sum = (await (await call("summary", filtersToParams({ ...base, code: "027" }))).json()) as Summary;
  assert.equal(sum.totalLeads, page.total);

  const bad = await call("leads", new URLSearchParams("code=12&page=0&sort=hax"));
  assert.equal(bad.status, 400);
  const body = await bad.json();
  assert.equal(body.error.code, "invalid_request");
  assert.ok(body.error.details.length >= 3);

  assert.equal((await call("secrets")).status, 404);
  assert.equal((await call("partners/..%2Fetc")).status, 404);
  assert.equal((await handleMisRequest(req("leads", undefined, "POST"), { config: { source: "demo" }, service: svc })).status, 405);
  assert.equal((await call("export", new URLSearchParams("format=pdf"))).status, 400);

  const p = await (await call("partners/027")).json();
  assert.deepEqual(p, { code: "027", state: "resolved", partnerName: "Demo Partner 027" });
  assert.equal((await (await call("partners/zz9")).json()).state, "unregistered");
  assert.equal((await (await call("partners/QQQ")).json()).state, "unknown");
  assert.equal((await call("partners/12")).status, 400);
  const meta = await (await call("meta")).json();
  assert.equal(meta.mode, "demo");
  assert.equal(meta.sync, null);                                                  // never claims a sync time it does not have
});

test("unexpected errors do not leak internals; production without a configured source is refused", async () => {
  const boom = { ...svc, leads: async () => { throw new Error("password=hunter2 at /srv/db.ts:12"); } };
  const r = await handleMisRequest(req("leads", leadsQueryToParams(lq())), { config: { source: "demo" }, service: boom });
  assert.equal(r.status, 500);
  const text = await r.text();
  assert.ok(!text.includes("hunter2") && !text.includes("/srv"));
  assert.equal(readConfig({ NODE_ENV: "production" }).source, "unconfigured");
  assert.equal(readConfig({ NODE_ENV: "development" }).source, "demo");
  assert.equal(readConfig({ MIS_DATA_SOURCE: "live", MIS_API_BASE_URL: "https://x.test" }).source, "unconfigured");   // key missing
  assert.equal(readConfig({ MIS_DATA_SOURCE: "live", MIS_API_BASE_URL: "ftp://x", MIS_API_KEY: "k" }).source, "unconfigured");
  const un = await handleMisRequest(req("meta"), { config: readConfig({ NODE_ENV: "production" }) });
  assert.equal(un.status, 503);
});

test("CSV export contains every matching row, not just a page, and honours filters/sort", async () => {
  const q = lq({ code: "027", sortKey: "externalLeadId", sortDir: "asc" });
  const total = (await svc.leads(q)).total;
  assert.ok(total > 25);                                                           // more than one page
  const r = await call("export", new URLSearchParams({ ...Object.fromEntries(leadsQueryToParams(q)), format: "csv" }));
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("x-export-row-count"), String(total));
  assert.match(r.headers.get("content-disposition")!, /lead-mis_027_entry_2026-08-01_2026-10-19\.csv/);
  const bytes = Buffer.from(await r.arrayBuffer());                                 // (Response.text() would hide the BOM)
  assert.deepEqual([...bytes.subarray(0, 3)], [0xef, 0xbb, 0xbf]);                  // UTF-8 BOM so Excel reads Unicode
  const text = bytes.toString("utf8");
  assert.ok(text.charCodeAt(0) === 0xfeff && text.slice(1).startsWith("Lead ID,"));
  // parse with a real CSV reader (python) to prove quoting/newlines survive
  const dir = mkdtempSync(join(tmpdir(), "csv-")); const f = join(dir, "o.csv"); writeFileSync(f, text);
  const py = spawnSync("python3", ["-c", `
import csv,json,sys
rows=list(csv.reader(open(sys.argv[1],encoding='utf-8-sig',newline='')))
print(json.dumps({'n':len(rows)-1,'w':sorted({len(r) for r in rows}),'codes':sorted({r[2] for r in rows[1:]}),'hdr':rows[0][:3],'ids':[r[0] for r in rows[1:4]],'cities':[r[17] for r in rows[1:] if '\\n' in r[17]]}))`, f], { encoding: "utf8" });
  assert.equal(py.status, 0, py.stderr);
  const out = JSON.parse(py.stdout);
  assert.equal(out.n, total); assert.deepEqual(out.w, [21]); assert.deepEqual(out.codes, ["027"]);
  assert.deepEqual(out.hdr, ["Lead ID", "ID Namespace", "Attribution Code"]);
  assert.deepEqual(out.ids.slice(0, 1), ["'=SUM(1,1)"]);                          // formula neutralised
  assert.deepEqual(out.cities, ['Mumbai, "East"\nZone']);                         // comma, quote and newline round-trip
});

test("Excel export: Summary + Leads sheets, all rows, typed cells, opens in openpyxl", async () => {
  const q = lq({ code: "27P", dateField: "report", statuses: ["disbursed", "rejected"], sortKey: "externalLeadId", sortDir: "asc" });
  const total = (await svc.leads(q)).total;
  const sum = await svc.summary(q);
  const r = await call("export", new URLSearchParams({ ...Object.fromEntries(leadsQueryToParams(q)), format: "xlsx" }));
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("x-export-row-count"), String(total));
  const dir = mkdtempSync(join(tmpdir(), "xlsx-")); const f = join(dir, "o.xlsx");
  writeFileSync(f, Buffer.from(await r.arrayBuffer()));
  const py = spawnSync("python3", ["-c", `
import json,sys,openpyxl,datetime
wb=openpyxl.load_workbook(sys.argv[1])
s=wb['Summary']; l=wb['Leads']
kv={str(r[0].value):r[1].value for r in s.iter_rows() if r[0].value is not None and len(r)>1}
rows=list(l.iter_rows(values_only=True))
hdr=rows[0]; data=rows[1:]
ei=hdr.index('Lead Entry Date'); di=hdr.index('Disbursed Amount (INR)')
print(json.dumps({'sheets':wb.sheetnames,'n':len(data),'kv':{k:(v if isinstance(v,(int,float,str)) else str(v)) for k,v in kv.items()},
 'ent_is_date':all(isinstance(r[ei],datetime.datetime) or r[ei] is None for r in data),
 'codes':sorted({r[2] for r in data}),'statuses':sorted({r[6] for r in data}),'freeze':l.freeze_panes,'filter':l.auto_filter.ref,
 'money_num':all(isinstance(r[di],(int,float)) or r[di] is None for r in data)}, default=str))`, f], { encoding: "utf8" });
  assert.equal(py.status, 0, py.stderr);
  const o = JSON.parse(py.stdout);
  assert.deepEqual(o.sheets, ["Summary", "Leads"]);
  assert.equal(o.n, total);
  assert.deepEqual(o.codes, ["27P"]);
  assert.ok(o.statuses.every((s: string) => ["disbursed", "rejected"].includes(s)));
  assert.equal(o.kv["Attribution code"], "27P");
  assert.equal(o.kv["Date basis"], "Report Date");                                 // selected date field preserved
  assert.equal(o.kv["Date from"], "2026-08-01"); assert.equal(o.kv["Date to"], "2026-10-19");
  assert.equal(o.kv["Lead status"], "disbursed, rejected");
  assert.equal(o.kv["State"], "All");
  assert.equal(o.kv["Partner"], "Demo Partner 27P");
  assert.equal(o.kv["Rows in Leads sheet"], total);
  assert.equal(o.kv["Total leads"], sum.totalLeads);                               // Summary sheet == dashboard metrics for the same filters
  assert.equal(o.kv["Total disbursed count"], sum.disbursedCount);
  assert.equal(o.ent_is_date, true); assert.equal(o.money_num, true);
  assert.equal(o.freeze, "A2"); assert.match(o.filter, /^A1:U\d+$/);
  assert.match(String(o.kv["Data source"]), /DEMO/);
});

test("Excel export neutralises formulas, keeps unicode/newlines, and the whole 6000-row set round-trips", async () => {
  const q = lq({ from: "2026-05-01", sortKey: "externalLeadId", sortDir: "asc", pageSize: 100 });
  const total = (await svc.leads(q)).total;
  const r = await call("export", new URLSearchParams({ ...Object.fromEntries(leadsQueryToParams(q)), format: "xlsx" }));
  const dir = mkdtempSync(join(tmpdir(), "xlsx-")); const f = join(dir, "o.xlsx");
  writeFileSync(f, Buffer.from(await r.arrayBuffer()));
  const py = spawnSync("python3", ["-c", `
import json,sys,openpyxl
wb=openpyxl.load_workbook(sys.argv[1],read_only=True); l=wb['Leads']
rows=list(l.iter_rows(values_only=True)); d=rows[1:]
print(json.dumps({'n':len(d),'eq':[r[0] for r in d if r[0] and r[0][0] in '=@+-'],'types':sorted({type(r[0]).__name__ for r in d}),
  'city':[r[17] for r in d if r[17] and ('\\n' in r[17] or 'पुणे' in r[17])]}))`, f], { encoding: "utf8" });
  assert.equal(py.status, 0, py.stderr);
  const o = JSON.parse(py.stdout);
  assert.equal(o.n, total);
  assert.deepEqual(o.types, ["str"]);                                               // stored as text, never a formula
  assert.ok(o.eq.includes("=SUM(1,1)") && o.eq.includes("@cmd|' /C calc'!A0"));
  assert.ok(o.city.includes('Mumbai, "East"\nZone') && o.city.includes("Pune – पुणे"));
});

test("live mode proxies with a server-side key, validates first, maps upstream failures", async () => {
  const seen: { url: string; key: string | undefined }[] = [];
  const stub = http.createServer((rq, rs) => {
    seen.push({ url: rq.url!, key: rq.headers["x-api-key"] as string | undefined });
    if (rq.url!.startsWith("/v1/mis/summary")) { rs.setHeader("content-type", "application/json"); rs.end(JSON.stringify({ totalLeads: 5 })); }
    else if (rq.url!.startsWith("/v1/mis/export")) { rs.setHeader("content-type", "text/csv"); rs.setHeader("x-export-row-count", "2"); rs.end("a,b\r\n1,2\r\n"); }
    else if (rq.url!.startsWith("/v1/mis/leads")) { rs.statusCode = 500; rs.end(JSON.stringify({ error: { message: "secret db dsn postgres://u:p@h/db" } })); }
    else if (rq.url!.startsWith("/v1/mis/meta")) { rs.statusCode = 401; rs.end("{}"); }
    else { rs.statusCode = 404; rs.end(); }
  });
  await new Promise<void>((res) => stub.listen(0, "127.0.0.1", res));
  const port = (stub.address() as { port: number }).port;
  const cfg = readConfig({ MIS_DATA_SOURCE: "live", MIS_API_BASE_URL: `http://127.0.0.1:${port}/`, MIS_API_KEY: "server-key" });
  assert.equal(cfg.source, "live");
  try {
    const s = await handleMisRequest(req("summary", filtersToParams(base)), { config: cfg });
    assert.equal(s.status, 200); assert.deepEqual(await s.json(), { totalLeads: 5 });
    assert.equal(seen[0].key, "server-key");                                       // key added by the BFF, not by the browser
    assert.ok(seen[0].url.startsWith("/v1/mis/summary?code=") === false && seen[0].url.includes("dateField=entry"));
    const before = seen.length;
    assert.equal((await handleMisRequest(req("summary", new URLSearchParams("code=1")), { config: cfg })).status, 400);
    assert.equal(seen.length, before);                                             // invalid input never reaches the backend
    const e = await handleMisRequest(req("export", new URLSearchParams({ ...Object.fromEntries(leadsQueryToParams(lq())), format: "csv" })), { config: cfg });
    assert.equal(e.headers.get("x-export-row-count"), "2"); assert.equal(await e.text(), "a,b\r\n1,2\r\n");
    const l = await handleMisRequest(req("leads", leadsQueryToParams(lq())), { config: cfg });
    assert.equal(l.status, 502);
    assert.ok(!(await l.text()).includes("postgres://"));                          // upstream internals are not relayed
    assert.equal((await handleMisRequest(req("meta"), { config: cfg })).status, 502);   // upstream auth failure is not shown as the user's 401
  } finally { stub.close(); }
  const dead = await handleMisRequest(req("meta"), { config: { source: "live", baseUrl: "http://127.0.0.1:1", apiKey: "k" } });
  assert.equal(dead.status, 502);
  assert.equal((await dead.json()).error.code, "upstream_unreachable");
});
