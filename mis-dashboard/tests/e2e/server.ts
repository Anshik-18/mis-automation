// Test server: static files + the REAL API handlers over the demo service, with fault injection. Test-only.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { createDemoService } from "../../src/server/demoService";
import { handleMisRequest } from "../../src/server/handlers";

const art = path.resolve(__dirname, "artifacts");
const NOW = new Date("2026-10-19T10:00:00Z");
const service = createDemoService({ now: () => NOW });
interface Fault { path: string; match?: string; mode: "error" | "delay" | "badshape" | "drop" | "empty"; ms?: number }
let faults: Fault[] = [];
let log: string[] = [];
const types: Record<string, string> = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html" };

http.createServer(async (req, res) => {
  const url = new URL(req.url!, "http://x");
  if (url.pathname === "/__ctl") {
    if (url.searchParams.has("reset")) { faults = []; log = []; }
    const f = url.searchParams.get("fault");
    if (f) faults.push(JSON.parse(f));
    res.setHeader("content-type", "application/json"); res.end(JSON.stringify({ ok: true })); return;
  }
  if (url.pathname === "/__log") { res.setHeader("content-type", "application/json"); res.end(JSON.stringify(log)); return; }
  if (url.pathname.startsWith("/api/mis/")) {
    const p = url.pathname.replace("/api/mis/", "");
    log.push(`${p}?${url.searchParams.toString()}`);
    const fault = faults.find((x) => p.startsWith(x.path) && (!x.match || url.search.includes(x.match)));
    if (fault?.mode === "drop") { req.socket.destroy(); return; }
    if (fault?.mode === "error") { res.statusCode = 500; res.setHeader("content-type", "application/json"); res.end(JSON.stringify({ error: { code: "internal", message: "Simulated server failure." } })); return; }
    if (fault?.mode === "badshape") { res.setHeader("content-type", "application/json"); res.end(JSON.stringify({ unexpected: true })); return; }
    if (fault?.mode === "empty") { res.statusCode = 200; res.setHeader("content-type", "text/csv"); res.end(""); return; }
    if (fault?.mode === "delay") await new Promise((r) => setTimeout(r, fault.ms ?? 500));
    const r = await handleMisRequest(new Request(`http://app.test${req.url}`, { method: req.method }), { config: { source: "demo" }, service });
    res.statusCode = r.status;
    r.headers.forEach((v, k) => res.setHeader(k, v));
    res.end(Buffer.from(await r.arrayBuffer()));
    return;
  }
  const file = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
  const fp = path.join(art, file);
  if (!fp.startsWith(art) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("not found"); return; }
  res.setHeader("content-type", types[path.extname(fp)] ?? "application/octet-stream");
  res.end(fs.readFileSync(fp));
}).listen(Number(process.env.PORT ?? 4173), "127.0.0.1", () => console.log("listening"));
