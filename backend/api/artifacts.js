import { ok, fail, gh, repo, query, preflight } from "./_lib.js";

export default async function handler(req, res) {
  if (preflight(req, res)) return;
  const q = query(req.url);
  const runId = q.get("run_id");
  if (!runId) return fail(res, 400, "run_id is required");

  try {
    const data = await gh(
      `/repos/${repo()}/actions/runs/${encodeURIComponent(runId)}/artifacts?per_page=100`,
    );
    ok(res, {
      ok: true,
      total: data.total_count,
      artifacts: (data.artifacts || []).map((a) => ({
        id: a.id,
        name: a.name,
        size_in_bytes: a.size_in_bytes,
        expired: a.expired,
        created_at: a.created_at,
        expires_at: a.expires_at,
        download_url: `/api/artifact-download?run_id=${encodeURIComponent(runId)}&id=${a.id}`,
      })),
    });
  } catch (e) {
    fail(res, e.status || 500, e.message);
  }
}
