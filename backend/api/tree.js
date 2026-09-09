import { ok, fail, gh, query, preflight, spineRepo, sourceRepo, repo } from "./_lib.js";

const ALLOWED = new Set(["leaopold", "vladleopold", "SYMBIOTYC", "SYMBlOTYC"]);

export default async function handler(req, res) {
  if (preflight(req, res)) return;
  const q = query(req.url);
  const target = q.get("repo") || spineRepo();
  const path = q.get("path") || "";
  const ref = q.get("ref") || "main";

  // Only ever browse repos this panel is meant to expose.
  const owner = target.split("/")[0];
  const defaults = [spineRepo(), sourceRepo(), repo()];
  if (!defaults.includes(target) && !ALLOWED.has(owner)) {
    return fail(res, 403, `repo not allowed: ${target}`);
  }

  try {
    const data = await gh(
      `/repos/${target}/contents/${encodeURIComponent(path)}?ref=${encodeURIComponent(ref)}`,
    );
    const list = Array.isArray(data) ? data : [data];
    ok(res, {
      ok: true,
      repo: target,
      path,
      ref,
      entries: list.map((e) => ({
        name: e.name,
        path: e.path,
        type: e.type === "dir" ? "dir" : "file",
        size: e.size || 0,
        sha: e.sha,
        download_url: e.download_url,
        html_url: e.html_url,
      })),
    });
  } catch (e) {
    if (e.status === 404) return ok(res, { ok: true, repo: target, path, ref, entries: [], empty: true });
    fail(res, e.status || 500, e.message);
  }
}
