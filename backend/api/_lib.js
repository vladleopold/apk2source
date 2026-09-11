/**
 * Shared helpers for the apk2source backend.
 *
 * The GitHub token lives here (Vercel env), never in the browser. Mutating
 * endpoints additionally require the shared panel key so a public Pages site
 * cannot be used to dispatch runs by anonymous visitors.
 */

export const GH_API = "https://api.github.com";

export function env(name, fallback = "") {
  const v = process.env[name];
  return v == null || v === "" ? fallback : v;
}

export function token() {
  return env("GITHUB_TOKEN") || env("APK2SOURCE_GITHUB_TOKEN");
}

export function repo() {
  return env("APK2SOURCE_REPO", "vladleopold/apk2source");
}

export function spineRepo() {
  return env("APK2SOURCE_SPINE_REPO", "vladleopold/source_spine");
}

export function sourceRepo() {
  return env("APK2SOURCE_SOURCE_REPO", "vladleopold/game_source");
}

export function json(res, status, body) {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.status(status).json(body);
}

export function ok(res, body) {
  json(res, 200, body);
}

export function fail(res, status, message, extra = {}) {
  json(res, status, { ok: false, error: message, ...extra });
}

/** Constant-time-ish comparison for the shared panel key. */
export function authorized(req) {
  const want = env("PANEL_ACCESS_KEY");
  if (!want) return true; // key not configured -> read-only mode enforced below
  const got = req.headers["x-panel-key"] || "";
  if (typeof got !== "string") return false;
  if (got.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i += 1) diff |= want.charCodeAt(i) ^ got.charCodeAt(i);
  return diff === 0;
}

export function hasPanelKey() {
  return Boolean(env("PANEL_ACCESS_KEY"));
}

export async function gh(path, { method = "GET", body, accept = "application/vnd.github+json", raw = false } = {}) {
  const t = token();
  const headers = {
    Accept: accept,
    "User-Agent": "apk2source-panel",
    "X-GitHub-Api-Version": "2022-11-28",
  };
  if (t) headers.Authorization = `Bearer ${t}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const r = await fetch(`${GH_API}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  if (!r.ok) {
    let detail = text.slice(0, 500);
    try { detail = JSON.parse(text).message || detail; } catch { /* keep raw */ }
    const e = new Error(`GitHub API ${r.status}: ${detail}`);
    e.status = r.status;
    throw e;
  }
  if (raw) return text;
  return text ? JSON.parse(text) : null;
}

export function query(url) {
  const u = new URL(url, "http://local");
  return u.searchParams;
}

export function preflight(req, res) {
  if (req.method === "OPTIONS") {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Panel-Key");
    res.setHeader("Access-Control-Max-Age", "86400");
    res.status(204).end("");
    return true;
  }
  return false;
}
