import { ok, fail, gh, repo, query, preflight } from "./_lib.js";

export default async function handler(req, res) {
  if (preflight(req, res)) return;
  const q = query(req.url);
  const perPage = Math.min(parseInt(q.get("per_page") || "50", 10) || 50, 100);
  const workflow = q.get("workflow") || "";
  const path = `/repos/${repo()}/actions/runs?per_page=${perPage}` +
    (workflow ? `&exclude_pull_requests=true` : "");

  try {
    const data = await gh(path);
    const runs = (data.workflow_runs || []).map((r) => ({
      run_id: String(r.id),
      id: r.id,
      name: r.name,
      display_title: r.display_title,
      status: r.status,
      conclusion: r.conclusion,
      event: r.event,
      html_url: r.html_url,
      created_at: r.created_at,
      updated_at: r.updated_at,
      run_started_at: r.run_started_at,
      run_attempt: r.run_attempt,
      head_branch: r.head_branch,
      actor: r.actor?.login || null,
      workflow_id: r.workflow_id,
    }));
    ok(res, { ok: true, total: data.total_count, runs });
  } catch (e) {
    fail(res, e.status || 500, e.message);
  }
}
