import { parseFilterParams, parseLeadsParams } from "../lib/filters";
import { checkCode } from "../lib/attribution";
import type { ExportFormat } from "../lib/types";
import { readConfig, type ServerConfig } from "./config";
import { createDemoService, type MisService } from "./demoService";
import { HttpError } from "./errors";
import { proxyToBackend } from "./proxy";

const SECURITY_HEADERS = { "cache-control": "no-store", "x-content-type-options": "nosniff" };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...SECURITY_HEADERS } });
}
function err(status: number, code: string, message: string, details?: string[]): Response {
  return json({ error: { code, message, ...(details ? { details } : {}) } }, status);
}

const ALLOWED = /^(meta|summary|leads|export|partners\/[0-9A-Za-z]{1,8})$/;

let demo: MisService | null = null;
function demoService(): MisService { return (demo ??= createDemoService()); }
export function resetDemoServiceForTests(): void { demo = null; }

export interface HandlerDeps { config?: ServerConfig; service?: MisService; fetchImpl?: typeof fetch }

/** Single entry point behind app/api/mis/[...path]/route.ts. Pure Request -> Response, so it is testable without Next. */
export async function handleMisRequest(req: Request, deps: HandlerDeps = {}): Promise<Response> {
  try {
    if (req.method !== "GET") return err(405, "method_not_allowed", "Only GET is supported.");
    const url = new URL(req.url);
    const path = url.pathname.replace(/^\/api\/mis\/?/, "").replace(/\/+$/, "");
    if (!ALLOWED.test(path)) return err(404, "not_found", "Unknown endpoint.");

    const cfg = deps.config ?? readConfig();
    if (cfg.source === "unconfigured") return err(503, "not_configured", cfg.reason ?? "The data source is not configured.");
    if (cfg.source === "live") {
      // Validate locally first so malformed input never reaches the backend.
      const bad = validateLocally(path, url.searchParams);
      if (bad) return bad;
      return proxyToBackend(path, url.search, cfg, deps.fetchImpl);
    }
    return await handleDemo(path, url.searchParams, deps.service ?? demoService());
  } catch (e) {
    if (e instanceof HttpError) return err(e.status, e.code, e.message, e.details);
    return err(500, "internal", "Something went wrong while handling the request.");   // no stack, no internals
  }
}

function validateLocally(path: string, sp: URLSearchParams): Response | null {
  if (path === "summary") { const r = parseFilterParams(sp); return r.ok ? null : err(400, "invalid_request", "Invalid filters.", r.errors); }
  if (path === "leads" || path === "export") { const r = parseLeadsParams(sp); return r.ok ? null : err(400, "invalid_request", "Invalid query.", r.errors); }
  if (path.startsWith("partners/")) { const c = checkCode(path.slice(9)); return c.ok && c.value ? null : err(400, "invalid_request", "Invalid attribution code."); }
  return null;
}

async function handleDemo(path: string, sp: URLSearchParams, svc: MisService): Promise<Response> {
  if (path === "meta") return json(await svc.meta());
  if (path === "summary") {
    const r = parseFilterParams(sp);
    return r.ok ? json(await svc.summary(r.value)) : err(400, "invalid_request", "Invalid filters.", r.errors);
  }
  if (path === "leads") {
    const r = parseLeadsParams(sp);
    return r.ok ? json(await svc.leads(r.value)) : err(400, "invalid_request", "Invalid query.", r.errors);
  }
  if (path === "export") {
    const format = sp.get("format") as ExportFormat | null;
    if (format !== "csv" && format !== "xlsx") return err(400, "invalid_request", "format must be csv or xlsx.");
    const r = parseLeadsParams(sp);
    if (!r.ok) return err(400, "invalid_request", "Invalid query.", r.errors);
    const f = await svc.exportFile(r.value, format);
    const body = typeof f.body === "string" ? f.body : new Uint8Array(f.body);
    return new Response(body, { status: 200, headers: {
      "content-type": f.contentType, "content-disposition": `attachment; filename="${f.filename}"`,
      "x-export-row-count": String(f.rowCount), ...SECURITY_HEADERS } });
  }
  const code = checkCode(path.slice("partners/".length));
  if (!code.ok || !code.value) return err(400, "invalid_request", "Invalid attribution code.");
  return json(await svc.partner(code.value));
}
