import { ok, fail, gh, repo, query, preflight } from "./_lib.js";

export default async function handler(req, res) {
  if (preflight(req, res)) return;
  const q = query(req.url);
  const runId = q.get("run_id") || q.get("id");
  if (!runId) return fail(res, 400, "run_id is required");

  try {
    const [run, jobsData] = await Promise.all([
      gh(`/repos/${repo()}/actions/runs/${encodeURIComponent(runId)}`),
      gh(`/repos/${repo()}/actions/runs/${encodeURIComponent(runId)}/jobs?per_page=100`),
    ]);

    const jobs = (jobsData.jobs || []).map((j) => ({
      id: j.id,
      name: j.name,
      status: j.status,
      conclusion: j.conclusion,
      started_at: j.started_at,
      completed_at: j.completed_at,
      html_url: j.html_url,
      runner_name: j.runner_name,
      steps: (j.steps || []).map((s) => ({
        name: s.name, status: s.status, conclusion: s.conclusion, number: s.number,
      })),
    }));

    ok(res, {
      ok: true,
      run: {
        run_id: String(run.id),
        name: run.name,
        display_title: run.display_title,
        status: run.status,
        conclusion: run.conclusion,
        event: run.event,
        html_url: run.html_url,
        created_at: run.created_at,
        updated_at: run.updated_at,
        run_started_at: run.run_started_at,
        head_branch: run.head_branch,
        actor: run.actor?.login || null,
      },
      jobs,
      ...Object.fromEntries(jobs.map((j) => [j.name, j])),
    });
  } catch (e) {
    fail(res, e.status || 500, e.message);
  }
}
