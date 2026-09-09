import { ok, fail, gh, repo, query, preflight, authorized, hasPanelKey, token } from "./_lib.js";

/**
 * POST /api/trigger
 * Dispatches `pipeline.yml` (or `stage.yml`) via workflow_dispatch.
 *
 * Body: { workflow?: string, ref?: string, inputs: { ... } }
 * Requires X-Panel-Key when PANEL_ACCESS_KEY is configured.
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

  const workflow = String(body.workflow || "pipeline.yml").replace(/[^\w.\-]/g, "");
  if (!/^(pipeline|stage|ci|pages)\.yml$/.test(workflow)) {
    return fail(res, 400, `workflow not allowed: ${workflow}`);
  }
  const ref = String(body.ref || "main").replace(/[^\w.\-/]/g, "");
  const inputs = body.inputs && typeof body.inputs === "object" ? body.inputs : {};

  // Whitelist + coerce to strings; workflow_dispatch inputs are all strings.
  const ALLOWED_KEYS = new Set([
    "url", "url_fallback", "game_name", "sha256", "use_cache", "run_device_cache",
    "run_java_decompile", "run_il2cpp", "run_assetripper", "publish_spine",
    "publish_game_source", "spine_repo", "source_repo", "max_texture_side", "runner",
    "stage",
  ]);
  const clean = {};
  for (const [k, v] of Object.entries(inputs)) {
    if (!ALLOWED_KEYS.has(k)) continue;
    if (v === null || v === undefined) continue;
    clean[k] = typeof v === "boolean" ? String(v) : String(v).slice(0, 2000);
  }
  if (!clean.url && workflow === "pipeline.yml") return fail(res, 400, "inputs.url is required");
  if (!clean.game_name && workflow === "pipeline.yml") return fail(res, 400, "inputs.game_name is required");

  try {
    await gh(`/repos/${repo()}/actions/workflows/${workflow}/dispatches`, {
      method: "POST",
      body: { ref, inputs: clean },
    });
  } catch (e) {
    return fail(res, e.status || 500, e.message, { workflow, ref });
  }

  // workflow_dispatch returns 204 with no run id; find the run we just created.
  let runUrl = null;
  let runId = null;
  try {
    await new Promise((r) => setTimeout(r, 2500));
    const data = await gh(`/repos/${repo()}/actions/runs?per_page=5&event=workflow_dispatch`);
    const hit = (data.workflow_runs || [])[0];
    if (hit) { runUrl = hit.html_url; runId = String(hit.id); }
  } catch { /* non-fatal */ }

  ok(res, { ok: true, workflow, ref, inputs: clean, run_id: runId, run_url: runUrl });
}
