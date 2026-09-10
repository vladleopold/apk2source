// apk2source Cloudflare Worker — GitHub API proxy + workflow dispatch
// Minimal serverless backend for the GitHub Pages control panel

const GH_API = "https://api.github.com"

export default {
  async fetch(request, env) {
    const GITHUB_TOKEN = env.GITHUB_TOKEN || ""
    const PANEL_KEY = env.PANEL_ACCESS_KEY || ""
    const REPO = env.APK2SOURCE_REPO || "vladleopold/apk2source"
    const url = new URL(request.url)
    const path = url.pathname.replace(/^\/api\//, "")

    // CORS preflight
    if (request.method === "OPTIONS") {
      return corsResponse(null, {})
    }

    // Health check
    if (path === "health") {
      return corsResponse({ ok: true, service: "apk2source-worker", time: new Date().toISOString() }, {})
    }

    // Config
    if (path === "config") {
      return corsResponse({
        ok: true,
        repository: REPO,
        spine_repo: env.APK2SOURCE_SPINE_REPO || "",
        source_repo: env.APK2SOURCE_SOURCE_REPO || "",
        panel_key_required: !!PANEL_KEY,
        stages: ["acquire","unpack","detect","unity","spine","publish-spine","publish-source","selftest"],
      }, {})
    }

    // Runs list
    if (path === "runs") {
      const perPage = Math.min(parseInt(url.searchParams.get("per_page") || "50"), 100)
      try {
        const data = await gh(`/repos/${REPO}/actions/runs?per_page=${perPage}`)
        const runs = (data.workflow_runs || []).map(r => ({
          run_id: String(r.id), id: r.id, name: r.name, display_title: r.display_title,
          status: r.status, conclusion: r.conclusion, event: r.event, html_url: r.html_url,
          created_at: r.created_at, updated_at: r.updated_at, run_started_at: r.run_started_at,
          head_branch: r.head_branch, actor: r.actor?.login || null,
        }))
        return corsResponse({ ok: true, total: data.total_count, runs }, {})
      } catch (e) { return fail(e.message, e.status || 500) }
    }

    // Single run + jobs
    if (path.startsWith("run/")) {
      const runId = path.split("/")[1]
      try {
        const [run, jobsData] = await Promise.all([
          gh(`/repos/${REPO}/actions/runs/${runId}`),
          gh(`/repos/${REPO}/actions/runs/${runId}/jobs?per_page=100`),
        ])
        const jobs = (jobsData.jobs || []).map(j => ({
          id: j.id, name: j.name, status: j.status, conclusion: j.conclusion,
          started_at: j.started_at, completed_at: j.completed_at, html_url: j.html_url,
          runner_name: j.runner_name, steps: (j.steps || []).map(s => ({
            name: s.name, status: s.status, conclusion: s.conclusion, number: s.number,
          })),
        }))
        return corsResponse({
          ok: true, run: { run_id: String(run.id), name: run.name, display_title: run.display_title,
            status: run.status, conclusion: run.conclusion, event: run.event, html_url: run.html_url,
            created_at: run.created_at, updated_at: run.updated_at, run_started_at: run.run_started_at,
            head_branch: run.head_branch, actor: run.actor?.login || null }, jobs }, {})
      } catch (e) { return fail(e.message, e.status || 500) }
    }

    // Artifacts
    if (path === "artifacts") {
      const runId = url.searchParams.get("run_id")
      if (!runId) return fail("run_id required", 400)
      try {
        const data = await gh(`/repos/${REPO}/actions/runs/${runId}/artifacts?per_page=100`)
        return corsResponse({ ok: true, total: data.total_count,
          artifacts: (data.artifacts || []).map(a => ({
            id: a.id, name: a.name, size_in_bytes: a.size_in_bytes, expired: a.expired,
            created_at: a.created_at, expires_at: a.expires_at,
            download_url: `/api/artifact-download?run_id=${runId}&id=${a.id}`,
          })) }, {})
      } catch (e) { return fail(e.message, e.status || 500) }
    }

    // Artifact download
    if (path === "artifact-download") {
      const runId = url.searchParams.get("run_id")
      const id = url.searchParams.get("id")
      if (!runId || !id) return fail("run_id and id required", 400)
      try {
        const r = await fetch(`${GH_API}/repos/${REPO}/actions/artifacts/${id}/zip`, {
          headers: { Authorization: `Bearer ${GITHUB_TOKEN}`, Accept: "application/zip" }
        })
        const body = await r.arrayBuffer()
        return new Response(body, {
          status: r.status, headers: { "Content-Type": "application/zip",
            "Content-Disposition": `attachment; filename="artifact-${id}.zip"` }
        })
      } catch (e) { return fail(e.message, e.status || 500) }
    }

    // Tree browser
    if (path === "tree") {
      const repo = url.searchParams.get("repo") || env.APK2SOURCE_SPINE_REPO || ""
      const treePath = url.searchParams.get("path") || ""
      const ref = url.searchParams.get("ref") || "main"
      const allowed = new Set([env.APK2SOURCE_SPINE_REPO, env.APK2SOURCE_SOURCE_REPO, REPO])
      if (!allowed.has(repo)) return fail(`repo not allowed: ${repo}`, 403)
      try {
        const data = await gh(`/repos/${repo}/contents/${encodeURIComponent(treePath)}?ref=${encodeURIComponent(ref)}`)
        const list = Array.isArray(data) ? data : [data]
        return corsResponse({ ok: true, repo, path: treePath, ref,
          entries: list.map(e => ({ name: e.name, path: e.path, type: e.type === "dir" ? "dir" : "file",
            size: e.size || 0, sha: e.sha, download_url: e.download_url, html_url: e.html_url })) }, {})
      } catch (e) {
        if (e.status === 404) return corsResponse({ ok: true, repo, path: treePath, ref, entries: [], empty: true }, {})
        return fail(e.message, e.status || 500)
      }
    }

    // Workflow dispatch
    if (path === "trigger") {
      if (request.method !== "POST") return fail("POST only", 405)
      if (!GITHUB_TOKEN) return fail("no GITHUB_TOKEN configured", 503)
      if (PANEL_KEY && !authorized(request)) return fail("invalid X-Panel-Key", 401)
      const body = await request.json()
      const workflow = (body.workflow || "pipeline.yml").replace(/[^\w.\-]/g, "")
      if (!/^(pipeline|stage|ci|pages)\.yml$/.test(workflow)) return fail(`workflow not allowed: ${workflow}`, 400)
      const inputs = {}
      const ALLOWED = new Set(["url","url_fallback","game_name","sha256","use_cache","run_device_cache",
        "run_java_decompile","run_il2cpp","run_assetripper","publish_spine","publish_game_source",
        "spine_repo","source_repo","max_texture_side","runner","stage"])
      for (const [k,v] of Object.entries(body.inputs || {})) {
        if (!ALLOWED.has(k)) continue
        inputs[k] = typeof v === "boolean" ? String(v) : String(v).slice(0, 2000)
      }
      if (!inputs.url && workflow === "pipeline.yml") return fail("inputs.url required", 400)
      if (!inputs.game_name && workflow === "pipeline.yml") return fail("inputs.game_name required", 400)
      try {
        await gh(`/repos/${REPO}/actions/workflows/${workflow}/dispatches`, {
          method: "POST", body: { ref: (body.ref || "main").slice(0, 100), inputs }
        })
        // Find the run we just created
        let runUrl = null, runId = null
        try {
          await new Promise(r => setTimeout(r, 2500))
          const data = await gh(`/repos/${REPO}/actions/runs?per_page=5&event=workflow_dispatch`)
          const hit = (data.workflow_runs || [])[0]
          if (hit) { runUrl = hit.html_url; runId = String(hit.id) }
        } catch { /* non-fatal */ }
        return corsResponse({ ok: true, workflow, ref: body.ref || "main", inputs, run_id: runId, run_url: runUrl }, {})
      } catch (e) { return fail(e.message, e.status || 500, { workflow }) }
    }

    // Stage dispatch
    if (path === "stage") {
      if (request.method !== "POST") return fail("POST only", 405)
      if (!GITHUB_TOKEN) return fail("no GITHUB_TOKEN configured", 503)
      if (PANEL_KEY && !authorized(request)) return fail("invalid X-Panel-Key", 401)
      const body = await request.json()
      const stage = (body.stage || "").toLowerCase()
      const STAGES = new Set(["acquire","unpack","detect","unity","spine","publish-spine","publish-source","selftest"])
      if (!STAGES.has(stage)) return fail(`unknown stage: ${stage}`, 400)
      const inputs = { stage, url: (body.url || "").slice(0, 2000),
        game_name: (body.game_name || "apk2source-selftest").slice(0, 120),
        use_cache: body.use_cache === false ? "false" : "true",
        runner: (body.runner || "ubuntu-latest").slice(0, 60),
        spine_repo: (body.spine_repo || "leaopold/source_spine").slice(0, 120),
        source_repo: (body.source_repo || "leaopold/game_source").slice(0, 120) }
      try {
        await gh(`/repos/${REPO}/actions/workflows/stage.yml/dispatches`, {
          method: "POST", body: { ref: (body.ref || "main").slice(0, 100), inputs }
        })
        let runUrl = null, runId = null
        try {
          await new Promise(r => setTimeout(r, 2500))
          const data = await gh(`/repos/${REPO}/actions/runs?per_page=5&event=workflow_dispatch`)
          const hit = (data.workflow_runs || [])[0]
          if (hit) { runUrl = hit.html_url; runId = String(hit.id) }
        } catch { /* non-fatal */ }
        return corsResponse({ ok: true, stage, inputs, run_id: runId, run_url: runUrl }, {})
      } catch (e) { return fail(e.message, e.status || 500, { stage }) }
    }

    return fail("not found", 404)
  }
}

// helpers
function authorized(req) {
  if (!PANEL_KEY) return true
  const got = req.headers.get("x-panel-key") || ""
  if (got.length !== PANEL_KEY.length) return false
  let diff = 0
  for (let i = 0; i < PANEL_KEY.length; i++) diff |= PANEL_KEY.charCodeAt(i) ^ got.charCodeAt(i)
  return diff === 0
}

async function gh(path, opts = {}) {
  const headers = { Accept: "application/vnd.github+json", "User-Agent": "apk2source-worker",
    "X-GitHub-Api-Version": "2022-11-28", ...opts.headers }
  if (GITHUB_TOKEN) headers.Authorization = `Bearer ${GITHUB_TOKEN}`
  if (opts.body) headers["Content-Type"] = "application/json"
  const r = await fetch(`${GH_API}${path}`, { method: opts.method || "GET", headers, body: opts.body ? JSON.stringify(opts.body) : undefined })
  const text = await r.text()
  if (!r.ok) { let msg = text.slice(0, 500); try { msg = JSON.parse(text).message || msg } catch {}
    const e = new Error(`GitHub API ${r.status}: ${msg}`); e.status = r.status; throw e }
  return text ? JSON.parse(text) : null
}

function corsResponse(body, headers = {}) {
  return new Response(JSON.stringify(body), {
    status: 200, headers: { "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, X-Panel-Key", "Access-Control-Max-Age": "86400", ...headers }
  })
}

function fail(message, status = 500, extra = {}) {
  return new Response(JSON.stringify({ ok: false, error: message, ...extra }), {
    status, headers: { "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*" }
  })
}
