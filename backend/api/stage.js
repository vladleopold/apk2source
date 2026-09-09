import { ok, fail, gh, repo, preflight, authorized, hasPanelKey, token } from "./_lib.js";

const STAGES = new Set([
  "acquire", "unpack", "detect", "unity", "spine",
  "publish-spine", "publish-source", "selftest",
]);

/**
 * POST /api/stage
 * Runs a single pipeline level through stage.yml (workflow_dispatch).
 * Body: { stage, url?, game_name?, use_cache?, runner? }
 */
export default async function handler(req, res) {
  if (preflight(req, res)) return;
  if (req.method !== "POST") return fail(res, 405, "POST only");
  if (!token()) return fail(res, 503, "backend has no GITHUB_TOKEN configured");
  if (hasPanelKey() && !authorized(req)) return fail(res, 401, "invalid or missing X-Panel-Key");

  let body = {};
  try {
    body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
  } catch {
    return fail(res, 400, "invalid JSON body");
  }

  const stage = String(body.stage || "");
  if (!STAGES.has(stage)) return fail(res, 400, `unknown stage: ${stage}`);

  const inputs = {
    stage,
    url: String(body.url || "").slice(0, 2000),
    game_name: String(body.game_name || "apk2source-selftest").slice(0, 120),
    use_cache: body.use_cache === false ? "false" : "true",
    runner: String(body.runner || "ubuntu-latest").slice(0, 60),
    spine_repo: String(body.spine_repo || "leaopold/source_spine").slice(0, 120),
    source_repo: String(body.source_repo || "leaopold/game_source").slice(0, 120),
  };

  try {
    await gh(`/repos/${repo()}/actions/workflows/stage.yml/dispatches`, {
      method: "POST",
      body: { ref: String(body.ref || "main"), inputs },
    });
  } catch (e) {
    return fail(res, e.status || 500, e.message, { stage });
  }

  let runUrl = null;
  let runId = null;
  try {
    await new Promise((r) => setTimeout(r, 2500));
    const data = await gh(`/repos/${repo()}/actions/runs?per_page=5&event=workflow_dispatch`);
    const hit = (data.workflow_runs || [])[0];
    if (hit) { runUrl = hit.html_url; runId = String(hit.id); }
  } catch { /* non-fatal */ }

  ok(res, { ok: true, stage, inputs, run_id: runId, run_url: runUrl });
}
