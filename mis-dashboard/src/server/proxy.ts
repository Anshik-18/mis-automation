import type { ServerConfig } from "./config";

const PASS_HEADERS = ["content-type", "content-disposition", "x-export-row-count"];

function errorJson(status: number, code: string, message: string): Response {
  return new Response(JSON.stringify({ error: { code, message } }), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

/**
 * Forwards an allow-listed GET to the MIS backend, adding the API key SERVER-SIDE. The browser never sees the key
 * or the backend URL. `path` has already been matched against the allow-list by the caller.
 */
export async function proxyToBackend(path: string, search: string, cfg: ServerConfig, fetchImpl: typeof fetch = fetch): Promise<Response> {
  const isExport = path === "export";
  let upstream: Response;
  try {
    upstream = await fetchImpl(`${cfg.baseUrl}/v1/mis/${path}${search}`, {
      method: "GET",
      headers: { "x-api-key": cfg.apiKey!, accept: isExport ? "*/*" : "application/json" },
      signal: AbortSignal.timeout(isExport ? 300_000 : 30_000),
      redirect: "error",
    });
  } catch {
    return errorJson(502, "upstream_unreachable", "The MIS backend could not be reached.");
  }
  if (!upstream.ok) {
    let message = "The MIS backend rejected the request.";
    let code = upstream.status >= 500 ? "upstream_error" : "upstream_rejected";
    let details: string[] | undefined;
    try {
      const j = await upstream.json();
      if (j?.error?.message && upstream.status < 500) message = String(j.error.message);
      if (j?.error?.code && upstream.status < 500) code = String(j.error.code);
      if (Array.isArray(j?.error?.details) && upstream.status < 500) details = j.error.details.map(String);
    } catch { /* body not JSON: keep the generic message */ }
    return new Response(JSON.stringify({ error: { code, message, ...(details ? { details } : {}) } }), {
      status: upstream.status === 401 || upstream.status === 403 ? 502 : upstream.status >= 500 ? 502 : upstream.status,
      headers: { "content-type": "application/json", "cache-control": "no-store" },
    });
  }
  const headers = new Headers({ "cache-control": "no-store", "x-content-type-options": "nosniff" });
  for (const h of PASS_HEADERS) { const v = upstream.headers.get(h); if (v) headers.set(h, v); }
  return new Response(upstream.body, { status: 200, headers });
}
