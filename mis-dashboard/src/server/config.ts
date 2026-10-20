export interface ServerConfig {
  source: "demo" | "live" | "unconfigured";
  baseUrl?: string;
  apiKey?: string;
  reason?: string;
}

/**
 * MIS_DATA_SOURCE=demo|live. In production the data source must be chosen explicitly - the app never falls back to
 * synthetic data by itself outside development.
 */
export function readConfig(env: Record<string, string | undefined> = process.env): ServerConfig {
  const src = (env.MIS_DATA_SOURCE ?? "").toLowerCase();
  if (src === "live") {
    if (!env.MIS_API_BASE_URL) return { source: "unconfigured", reason: "MIS_DATA_SOURCE=live needs MIS_API_BASE_URL." };
    if (!env.MIS_API_KEY) return { source: "unconfigured", reason: "MIS_DATA_SOURCE=live needs MIS_API_KEY (server-side only)." };
    let url: URL;
    try { url = new URL(env.MIS_API_BASE_URL); } catch { return { source: "unconfigured", reason: "MIS_API_BASE_URL is not a valid URL." }; }
    if (url.protocol !== "https:" && url.protocol !== "http:") return { source: "unconfigured", reason: "MIS_API_BASE_URL must be http(s)." };
    return { source: "live", baseUrl: env.MIS_API_BASE_URL.replace(/\/+$/, ""), apiKey: env.MIS_API_KEY };
  }
  if (src === "demo") return { source: "demo" };
  if (src === "" && env.NODE_ENV !== "production") return { source: "demo" };
  return { source: "unconfigured", reason: "Set MIS_DATA_SOURCE to 'live' (with MIS_API_BASE_URL and MIS_API_KEY) or 'demo'." };
}
