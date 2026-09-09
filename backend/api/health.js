import { ok, token, repo, spineRepo, sourceRepo, hasPanelKey, gh, preflight } from "./_lib.js";

export default async function handler(req, res) {
  if (preflight(req, res)) return;

  const t = token();
  let authed = false;
  let login = null;
  let rate = null;
  if (t) {
    try {
      const u = await gh("/user");
      authed = true;
      login = u.login;
    } catch { /* token invalid or missing scope */ }
    try {
      const rl = await gh("/rate_limit");
      rate = rl?.resources?.core ?? null;
    } catch { /* ignore */ }
  }

  ok(res, {
    ok: true,
    service: "apk2source-backend",
    version: "1.0.0",
    time: new Date().toISOString(),
    github: { configured: Boolean(t), authed, login, rate },
    repos: { pipeline: repo(), spine: spineRepo(), source: sourceRepo() },
    security: { panel_key_required: hasPanelKey() },
    endpoints: [
      "GET  /api/health",
      "GET  /api/config",
      "GET  /api/runs?per_page=50",
      "GET  /api/run/:id",
      "GET  /api/artifacts?run_id=",
      "GET  /api/tree?repo=&path=",
      "POST /api/trigger   (requires X-Panel-Key when PANEL_ACCESS_KEY is set)",
      "POST /api/stage     (requires X-Panel-Key when PANEL_ACCESS_KEY is set)",
    ],
  });
}
