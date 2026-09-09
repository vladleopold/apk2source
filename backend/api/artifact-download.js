import { fail, gh, repo, query, preflight } from "./_lib.js";

/** GET /api/artifact-download?run_id=&id= -> 302 to the signed GitHub URL. */
export default async function handler(req, res) {
  if (preflight(req, res)) return;
  const q = query(req.url);
  const id = q.get("id");
  if (!id) return fail(res, 400, "id is required");

  try {
    const data = await gh(
      `/repos/${repo()}/actions/artifacts/${encodeURIComponent(id)}/zip`,
      { raw: true },
    );
    // The API answers a dispatch request with a redirect; fetch followed it and
    // handed us the archive body. Stream it straight back to the caller.
    res.setHeader("Content-Type", "application/zip");
    res.setHeader("Content-Disposition", `attachment; filename="artifact-${id}.zip"`);
    res.status(200).send(data);
  } catch (e) {
    fail(res, e.status || 500, e.message);
  }
}
