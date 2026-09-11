/* apk2source control panel - dependency-free vanilla JS.
 *
 * Reads run history from ./data/*.json (baked at Pages build time) and from the
 * optional Vercel backend (live). Mutating actions (workflow_dispatch) always go
 * through the backend so the GitHub token never reaches the browser.
 */
(() => {
  "use strict";
  const LS_BACKEND = "apk2source.backend";
  const LS_KEY = "apk2source.key";
  const REPO = "vladleopold/apk2source";
  const SPINE_REPO = "leaopold/source_spine";
  const SOURCE_REPO = "leaopold/game_source";

  const DEFAULT_BACKENDS = [
    "https://apk2source-api-v3.leopolds2010.workers.dev",
  ];

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  const state = {
    config: {},
    backend: "",  // Start fresh; detectBackend will set correct URL
    key: localStorage.getItem(LS_KEY) || "d6d0e1b39714d1304018c209bbf0e352d57b99ff6de02eac",
    backendOk: false,
    runs: [],
    timer: null,
  };

  // Clear stale backend from localStorage
  localStorage.removeItem(LS_BACKEND);

  // ---------------------------------------------------------------- helpers
  const fmtDur = (ms) => {
    if (!ms || ms < 0) return "—";
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
  };

  const fmtDate = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };

  const fmtBytes = (n) => {
    if (!n && n !== 0) return "—";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
  };

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  function setMsg(el, text, kind) {
    el.textContent = text || "";
    el.className = `msg${kind ? ` ${kind}` : ""}`;
  }

  // ---------------------------------------------------------------- backend
  async function api(path, opts = {}) {
    // Static mode: serve from baked panel-data/index.json when no backend
    if (!state.backend) {
      const base = (location.pathname.replace(/\/[^\/]*$/, "") || "") + "/data";
      if (path === "/api/config") {
        return { ok: true, repository: REPO, spine_repo: SPINE_REPO, source_repo: SOURCE_REPO,
          stages: ["acquire","unpack","detect","unity","spine","publish-spine","publish-source","selftest"],
          panel_key_required: false };
      }
      if (path === "/api/runs") {
        const idx = await loadStatic(`${base}/index.json`, { runs: [] });
        return { ok: true, total: idx.runs.length, runs: idx.runs.map(r => ({
          run_id: String(r.run_id), id: Number(r.id), name: r.name, display_title: r.display_title,
          status: r.status, conclusion: r.conclusion, event: r.event, html_url: r.html_url,
          created_at: r.created_at, updated_at: r.updated_at, run_started_at: r.run_started_at,
          head_branch: r.head_branch, actor: r.actor
        })) };
      }
      if (path.startsWith("/api/run/")) {
        const runId = path.split("/")[1];
        const idx = await loadStatic(`${base}/index.json`, { runs: [] });
        const run = idx.runs.find(r => String(r.run_id) === runId);
        if (!run) throw new Error("run not found");
        return { ok: true, run: { run_id: String(run.run_id), name: run.name, display_title: run.display_title,
            status: run.status, conclusion: run.conclusion, event: run.event, html_url: run.html_url,
            created_at: run.created_at, updated_at: run.updated_at, run_started_at: run.run_started_at,
            head_branch: run.head_branch, actor: run.actor }, jobs: [] };
      }
      if (path === "/api/tree") throw new Error("tree browsing requires backend");
      if (opts && opts.method === "POST") throw new Error("write operations require backend. Use GitHub Actions UI.");
      throw new Error("static mode: unsupported endpoint");
    }
    // Dynamic mode: use backend with fallback to next backend on 403/503
    const headers = { Accept: "application/json", ...(opts.headers || {}) };
    if (opts.body) headers["Content-Type"] = "application/json";
    if (state.key) headers["X-Panel-Key"] = state.key;
    
    let lastErr = null;
    for (const candidate of [state.backend, ...DEFAULT_BACKENDS].filter(Boolean)) {
      try {
        const url = `${candidate.replace(/\/$/, "")}${path}`;
        const r = await fetch(url, {
          ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined,
        });
        const text = await r.text();
        let data = null;
        try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
        if (!r.ok) {
          const msg = (data && (data.error || data.message)) || `HTTP ${r.status}`;
          if ((r.status === 403 || r.status === 503)) {
            // Backend unavailable, try next in list
            lastErr = new Error(msg);
            continue;
          }
          throw new Error(msg);
        }
        return data;
      } catch (e) {
        lastErr = e;
      }
    }
    // All backends failed — clear bad backend from localStorage and serve from baked data
    if (opts && opts.method !== "POST") {
      const base = (location.pathname.replace(/\/[^\/]*$/, "") || "") + "/data";
      if (path === "/api/runs" || path.startsWith("/api/run/")) {
        const idx = await loadStatic(`${base}/index.json`, { runs: [] });
        if (path === "/api/runs") {
          return { ok: true, total: idx.runs.length, runs: idx.runs.map(r => ({
            run_id: String(r.run_id), id: Number(r.id), name: r.name, display_title: r.display_title,
            status: r.status, conclusion: r.conclusion, event: r.event, html_url: r.html_url,
            created_at: r.created_at, updated_at: r.updated_at, run_started_at: r.run_started_at,
            head_branch: r.head_branch, actor: r.actor
          })) };
        }
        const runId = path.split("/")[1];
        const run = idx.runs.find(r => String(r.run_id) === runId);
        if (run) {
          return { ok: true, run: { run_id: String(run.run_id), name: run.name, display_title: run.display_title,
              status: run.status, conclusion: run.conclusion, event: run.event, html_url: run.html_url,
              created_at: run.created_at, updated_at: run.updated_at, run_started_at: run.run_started_at,
              head_branch: run.head_branch, actor: run.actor }, jobs: [] };
        }
      }
    }
    throw lastErr || new Error("all backends failed");
  }

  async function detectBackend() {
    const pill = $("#backend-pill");
    // Try DEFAULT_BACKENDS only — never trust stale candidates
    for (const url of DEFAULT_BACKENDS) {
      try {
        const r = await fetch(`${url}/api/health`, { method: "GET", headers: { Accept: "application/json" } });
        if (r.ok) {
          state.backend = url;
          state.backendOk = true;
          localStorage.setItem(LS_BACKEND, url);
          pill.textContent = `backend: ${url.replace(/^https?:\/\//, "").split("/")[0]}`;
          pill.className = "pill pill-ok";
          return true;
        }
      } catch { /* try next */ }
    }
    localStorage.removeItem(LS_BACKEND);
    state.backendOk = false;
    state.backend = DEFAULT_BACKENDS[0] || "";
    pill.textContent = "backend: offline (static data only)";
    pill.className = "pill pill-err";
    return false;
  }

  // ---------------------------------------------------------------- config
  async function loadStatic(path, fallback) {
    try {
      const r = await fetch(path, { cache: "no-store" });
      if (!r.ok) return fallback;
      return await r.json();
    } catch { return fallback; }
  }

  async function loadConfig() {
    const base = (location.pathname.replace(/\/[^/]*$/, "") || "") + "/data";
    state.config = await loadStatic(`${base}/config.json`, {});
    $("#repo-pill").textContent = `repo: ${state.config.repository || "—"}`;
    $("#built-at").textContent = state.config.built_at ? `built ${fmtDate(state.config.built_at)}` : "";
    const cfgSpine = $("#cfg-spine-repo");
    if (cfgSpine) cfgSpine.textContent = state.config.spine_repo || "source_spine";
    const cfgSource = $("#cfg-source-repo");
    if (cfgSource) cfgSource.textContent = state.config.source_repo || "game_source";
    return state.config;
  }

  // ---------------------------------------------------------------- tabs
  function initTabs() {
    $$(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        $$(".tab").forEach((t) => { t.classList.remove("active"); t.setAttribute("aria-selected", "false"); });
        $$(".panel").forEach((p) => p.classList.remove("active"));
        tab.classList.add("active");
        tab.setAttribute("aria-selected", "true");
        const panel = $(`#tab-${tab.dataset.tab}`);
        if (panel) panel.classList.add("active");
        if (tab.dataset.tab === "history") loadRuns();
        if (tab.dataset.tab === "spine") loadSpine();
      });
    });
  }

  // ---------------------------------------------------------------- DAG
  const STAGES = [
    { level: 0, cls: "n-plan", id: "plan", name: "plan", desc: "resolve inputs, slug, cache key, bus", io: ["dispatch inputs", "plan outputs"], runner: "ubuntu" },
    { level: 1, cls: "n-heavy", id: "acquire", name: "01 acquire", desc: "resumable download + sha256 + cache", io: ["url", "payload"], runner: "ubuntu" },
    { level: 1, cls: "n-heavy", id: "unpack", name: "02 unpack", desc: ".apks/.xapk/.apkm/.aab → merged tree", io: ["payload", "merged/ + splits/"], runner: "ubuntu" },
    { level: 1, cls: "n-heavy", id: "detect", name: "02b detect", desc: "engine fingerprint + spine signals", io: ["merged/", "engine.json"], runner: "ubuntu" },
    { level: 1, cls: "n-heavy", id: "unity", name: "04 unity", desc: "UnityPy → asset tree (container paths kept)", io: ["merged/", "assets/"], runner: "ubuntu" },
    { level: 1, cls: "n-heavy", id: "spine", name: "05 spine", desc: "sniff json/skel/atlas/png, pair, export", io: ["assets/", "spine/<game>/"], runner: "ubuntu" },
    { level: 2, cls: "n-par", id: "device-cache", name: "03 device cache", desc: "emulator install → first run → snapshot diff", io: ["splits/", "cache/"], runner: "macos" },
    { level: 2, cls: "n-par", id: "java-decompile", name: "04a jadx/apktool", desc: "dex → Java, resources.arsc → xml", io: ["base.apk", "java-source"], runner: "ubuntu" },
    { level: 2, cls: "n-par", id: "il2cpp-decompile", name: "04b il2cppdumper", desc: "libil2cpp.so + metadata → C# signatures", io: ["merged/", "DummyDll"], runner: "ubuntu" },
    { level: 2, cls: "n-par", id: "unity-project", name: "04c AssetRipper", desc: "reconstruct an openable Unity project", io: ["merged/", "ExportedProject"], runner: "ubuntu" },
    { level: 3, cls: "n-pub", id: "spine-publish", name: "06 publish spine", desc: "sparse clone → commit → push", io: ["spine/<game>/", "source_spine"], runner: "ubuntu" },
    { level: 3, cls: "n-pub", id: "source-publish", name: "06 publish source", desc: "sparse clone → commit → push", io: ["ExportedProject", "game_source"], runner: "ubuntu" },
    { level: 4, cls: "n-rep", id: "report", name: "07 report", desc: "pipeline.json → Pages panel data", io: ["all states", "panel-data/"], runner: "ubuntu" },
  ];

  function renderDag(jobStates = {}) {
    const dag = $("#live-dag") || $("#dag");
    if (!dag) return;
    const stOf = (id) => jobStates[id] || "idle";
    // Visible only: running + failed + exactly one "next" box in pipeline order.
    // Finished (ok/skip) and idle boxes are hidden.
    let nextShown = false;
    const isVisible = (n) => {
      const st = stOf(n.id);
      if (st === "run" || st === "fail") return true;
      if ((st === "wait" || st === "idle") && !nextShown) { nextShown = true; return true; }
      return false;
    };
    const levels = [...new Set(STAGES.map((s) => s.level))].sort();
    dag.innerHTML = levels.map((lv) => {
      const nodes = STAGES.filter((s) => s.level === lv && isVisible(s));
      if (!nodes.length) return "";
      return `<div class="dag-level">
        <div class="lvl-title">level ${lv}${lv === 2 ? " · parallel" : ""}</div>
        ${nodes.map((n) => {
          const st = stOf(n.id);
          const nodeCls = st === "run" ? "is-run" : st === "fail" ? "is-fail" : "";
          return `<div class="node ${n.cls} ${nodeCls}" data-stage="${n.id}">
            <span class="st st-${st}">${esc(st)}</span>
            <b>${esc(n.name)}</b><small>${esc(n.desc)}</small>
          </div>`;
        }).join("")}
      </div>`;
    }).join("");

    const tb = $("#stage-table tbody");
    if (tb) {
      tb.innerHTML = STAGES.map((n, i) => `<tr>
        <td class="mono">${i + 1}</td>
        <td><b>${esc(n.name)}</b><br><small style="color:var(--fg-dim)">${esc(n.desc)}</small></td>
        <td class="mono">${esc(n.id)}</td>
        <td class="mono">${esc(n.io[0])}</td>
        <td class="mono">${esc(n.io[1])}</td>
        <td class="mono">${esc(n.runner)}</td>
      </tr>`).join("");
    }
  }

  function matchStage(jobName) {
    const key = (jobName || "").toLowerCase();
    const match = STAGES.find((s) => key.includes(s.id) || key.includes(s.name.split(" ").slice(-1)[0]));
    return match ? match.id : null;
  }

  // Stage patterns: the heavy job covers 5 stages, so map by STEP names first.
  const STAGE_PATTERNS = [
    ["plan", /plan/],
    ["acquire", /acquire/],
    ["unpack", /unpack/],
    ["detect", /detect/],
    ["unity", /unity asset|asset extraction/],
    ["spine", /spine extraction/],
    ["device-cache", /capture cache|device cache/],
    ["java-decompile", /jadx|apktool|java \/ resources/],
    ["il2cpp-decompile", /il2cpp/],
    ["unity-project", /assetripper|unity project/],
    ["spine-publish", /publish.*spine|source_spine/],
    ["source-publish", /game.?source/],
    ["report", /report/],
  ];

  function stepState(step) {
    if (!step) return "wait";
    if (step.status === "completed") {
      if (step.conclusion === "success") return "ok";
      if (step.conclusion === "skipped") return "skip";
      return "fail";
    }
    return step.status === "in_progress" ? "run" : "wait";
  }

  function matchStagePattern(name) {
    const key = (name || "").toLowerCase();
    const hit = STAGE_PATTERNS.find(([, re]) => re.test(key));
    return hit ? hit[0] : null;
  }

  const STATE_RANK = { run: 5, fail: 4, wait: 3, ok: 2, skip: 1, idle: 0 };
  function mergeState(prev, next) {
    if (!prev) return next;
    return (STATE_RANK[next] || 0) > (STATE_RANK[prev] || 0) ? next : prev;
  }

  function jobStatesFromRun(run) {
    const out = {};
    if (!run || !run.jobs) return out;
    for (const j of run.jobs) {
      // Precise: map every step (covers the 5 stages inside the heavy job).
      for (const s of j.steps || []) {
        const id = matchStagePattern(s.name);
        if (!id) continue;
        out[id] = mergeState(out[id], stepState(s));
      }
      // Fallback: whole-job mapping when no step matched.
      const id = matchStage(j.name) || matchStagePattern(j.name);
      if (!id) continue;
      const st = j.status === "completed"
        ? (j.conclusion === "success" ? "ok" : j.conclusion === "skipped" ? "skip" : "fail")
        : (j.status === "in_progress" ? "run" : "wait");
      out[id] = mergeState(out[id], st);
    }
    return out;
  }

  // ---------------------------------------------------------------- live run tracker
  // After dispatch the Run tab becomes a live dashboard: DAG boxes light up,
  // the finished ones scroll left, and one short log line ticks below.
  const live = { runId: null, timer: null };

  function setLiveBadge(txt) {
    const b = $("#live-badge");
    if (b) { b.textContent = txt; b.className = `badge b-${txt}`; }
  }

  function setLiveLog(txt) {
    const el = $("#live-log-text");
    if (el) el.textContent = txt;
  }

  function pickLogLine(lines) {
    if (!lines || !lines.length) return null;
    for (let i = lines.length - 1; i >= 0; i--) {
      const l = (lines[i] || "").trim();
      if (!l || /^##\[endgroup\]/.test(l)) continue;
      return l.length > 220 ? `${l.slice(0, 220)}…` : l;
    }
    return null;
  }

  function startLive(runId, runUrl, title) {
    stopLive();
    if (!runId) return;
    live.runId = String(runId);
    const card = $("#live");
    if (card) card.hidden = false;
    const form = $("#run-form");
    if (form) form.hidden = true;
    const t = $("#live-title");
    if (t) t.textContent = title || `run #${live.runId}`;
    const link = $("#live-link");
    if (link) {
      if (runUrl) { link.href = runUrl; link.hidden = false; }
      else link.hidden = true;
    }
    setLiveBadge("queued");
    setLiveLog("starting…");
    renderDag({});
    pollLive();
    live.timer = setInterval(pollLive, 8000);
  }

  function stopLive() {
    if (live.timer) clearInterval(live.timer);
    live.timer = null;
    live.runId = null;
  }

  function pauseLive() {
    if (live.timer) clearInterval(live.timer);
    live.timer = null;
  }

  async function pollLive() {
    if (!live.runId || !state.backendOk) return;
    try {
      const data = await api(`/api/run/${encodeURIComponent(live.runId)}`);
      const run = data.run || {};
      renderDag(jobStatesFromRun(data));
      const concl = run.conclusion || run.status || "…";
      setLiveBadge(concl);
      const sub = $("#live-sub");
      if (sub) sub.textContent = `#${live.runId} · ${run.name || ""} · ${fmtDate(run.run_started_at || run.created_at)}`;
      const jobs = data.jobs || [];
      const active = jobs.find((j) => j.status === "in_progress")
        || [...jobs].reverse().find((j) => ["queued", "waiting", "pending"].includes(j.status))
        || null;
      if (active) {
        const stageId = matchStage(active.name);
        if (stageId) {
          const node = document.querySelector(`#live-dag .node[data-stage="${stageId}"]`);
          if (node) node.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
        }
      }
      if (active && active.id) {
        const steps = active.steps || [];
        const step = steps.filter((s) => s.status === "in_progress").slice(-1)[0] || steps.slice(-1)[0];
        const stepTxt = step ? ` · ${step.name}` : "";
        try {
          const log = await api(`/api/job-log?job_id=${active.id}&tail=25`);
          const line = pickLogLine(log.lines);
          setLiveLog(`${active.name}${stepTxt} — ${line || "running…"}`);
        } catch {
          setLiveLog(`${active.name}${stepTxt} — running…`);
        }
      } else if (run.status === "completed") {
        const done = jobs.filter((j) => j.conclusion === "success").length;
        setLiveLog(`finished: ${run.conclusion} · ${done}/${jobs.length} jobs ok`);
        pauseLive();
      } else {
        setLiveLog("queued — waiting for runner…");
      }
    } catch (e) {
      setLiveLog(`tracking error: ${e.message}`);
    }
  }

  // ---------------------------------------------------------------- runs
  async function loadRuns() {
    const tbody = $("#runs-table tbody");
    const empty = $("#runs-empty");
    tbody.innerHTML = "";
    empty.textContent = "Loading…";
    empty.style.display = "block";

    let runs = [];
    if (state.backendOk) {
      try { runs = (await api("/api/runs?per_page=50")).runs || []; } catch (e) { console.warn(e); }
    }
    if (!runs.length) {
      const base = (location.pathname.replace(/\/[^/]*$/, "") || "") + "/data";
      const idx = await loadStatic(`${base}/index.json`, { runs: [] });
      runs = idx.runs || [];
    }
    state.runs = runs;

    if (!runs.length) {
      empty.textContent = state.backendOk
        ? "No workflow runs yet. Dispatch one from the Run tab."
        : "No baked run data and the backend is offline. Configure a backend in ⚙ Settings.";
      return;
    }
    empty.style.display = "none";

    tbody.innerHTML = runs.map((r) => {
      const start = r.run_started_at || r.created_at;
      const end = r.updated_at;
      const dur = start && end ? new Date(end) - new Date(start) : null;
      const concl = r.conclusion || r.status || "—";
      return `<tr>
        <td class="mono"><a href="${esc(r.html_url || "#")}" target="_blank" rel="noopener">#${esc(r.run_id || r.id)}</a></td>
        <td>${esc(r.name || "—")}</td>
        <td>${esc(r.display_title || "—")}</td>
        <td class="mono">${esc(r.event || "—")}</td>
        <td><span class="badge b-${esc(concl)}">${esc(concl)}</span></td>
        <td>${fmtDate(start)}</td>
        <td class="mono">${fmtDur(dur)}</td>
        <td><button class="btn btn-ghost" data-run="${esc(r.run_id || r.id)}" type="button">detail</button></td>
      </tr>`;
    }).join("");

    $$("[data-run]", tbody).forEach((b) => b.addEventListener("click", () => {
      const run = state.runs.find((r) => String(r.run_id || r.id) === b.dataset.run);
      switchTab("run");
      startLive(b.dataset.run, run && run.html_url, run && (run.display_title || run.name));
    }));
  }

  function switchTab(name) {
    const tab = document.querySelector(`.tab[data-tab="${name}"]`);
    if (tab) tab.click();
  }

  function renderLatest(run) {
    const el = $("#latest-run");
    if (!el) return;
    if (!run) { el.innerHTML = '<div class="empty">No run data yet.</div>'; return; }
    const concl = run.conclusion || run.status || "—";
    el.innerHTML = `
      <div class="row" style="justify-content:space-between;align-items:flex-start">
        <div>
          <div style="font-size:16px;font-weight:600">${esc(run.display_title || run.name || "run")}</div>
          <div class="mono" style="color:var(--fg-dim);font-size:12px">
            #${esc(run.run_id || run.id)} · ${esc(run.name || "")} · ${esc(run.event || "")}
          </div>
        </div>
        <div style="text-align:right">
          <span class="badge b-${esc(concl)}">${esc(concl)}</span>
          <div class="mono" style="color:var(--fg-dim);font-size:12px;margin-top:4px">${fmtDate(run.run_started_at || run.created_at)}</div>
        </div>
      </div>
      ${run.html_url ? `<div style="margin-top:10px"><a href="${esc(run.html_url)}" target="_blank" rel="noopener">Open in GitHub →</a></div>` : ""}
      <div id="latest-jobs" style="margin-top:12px"></div>`;
    showRun(run.run_id || run.id, true);
  }

  async function showRun(runId, quiet = false) {
    if (!state.backendOk) {
      if (!quiet) renderDag({});
      return;
    }
    try {
      const run = await api(`/api/run/${encodeURIComponent(runId)}`);
      renderDag(jobStatesFromRun(run));
      const box = $("#latest-jobs");
      if (box && run.jobs) {
        box.innerHTML = `<h3>Jobs</h3><div class="table-wrap"><table>
          <thead><tr><th>Job</th><th>Status</th><th>Duration</th><th></th></tr></thead>
          <tbody>${run.jobs.map((j) => {
            const c = j.conclusion || j.status || "—";
            const d = j.started_at && j.completed_at ? new Date(j.completed_at) - new Date(j.started_at) : null;
            return `<tr><td>${esc(j.name)}</td>
              <td><span class="badge b-${esc(c)}">${esc(c)}</span></td>
              <td class="mono">${fmtDur(d)}</td>
              <td>${j.html_url ? `<a href="${esc(j.html_url)}" target="_blank" rel="noopener">logs</a>` : ""}</td></tr>`;
          }).join("")}</tbody></table></div>`;
      }
    } catch (e) {
      if (!quiet) console.warn("showRun failed", e);
    }
  }

  // ---------------------------------------------------------------- spine library
  async function loadSpine() {
    const tree = $("#spine-tree");
    const sel = $("#spine-repo-select");
    const repos = [state.config.spine_repo || "leaopold/source_spine", state.config.source_repo || "leaopold/game_source"];
    if (!sel.options.length) {
      sel.innerHTML = repos.map((r) => `<option value="${esc(r)}">${esc(r)}</option>`).join("");
      sel.addEventListener("change", loadSpine);
    }
    const repo = sel.value || repos[0];
    tree.className = "tree empty";
    tree.textContent = "Loading…";

    if (!state.backendOk) {
      tree.textContent = "Backend offline — configure it in ⚙ Settings to browse the destination repos.";
      return;
    }
    try {
      const data = await api(`/api/tree?repo=${encodeURIComponent(repo)}&path=`);
      tree.className = "tree";
      tree.innerHTML = renderTree(data.entries || [], repo, "");
      bindTree(tree, repo);
    } catch (e) {
      tree.className = "tree empty";
      tree.textContent = `Failed: ${e.message}`;
    }
  }

  function renderTree(entries, repo, path) {
    if (!entries.length) return '<div class="empty">empty</div>';
    const dirs = entries.filter((e) => e.type === "dir").sort((a, b) => a.name.localeCompare(b.name));
    const files = entries.filter((e) => e.type !== "dir").sort((a, b) => a.name.localeCompare(b.name));
    const items = [...dirs, ...files].map((e) => {
      const full = path ? `${path}/${e.path || e.name}` : (e.path || e.name);
      if (e.type === "dir") {
        return `<li><span class="dir" data-path="${esc(full)}">${esc(e.name)}</span><ul hidden></ul></li>`;
      }
      const href = `https://github.com/${repo}/blob/main/${full}`;
      return `<li><a class="file" href="${esc(href)}" target="_blank" rel="noopener">${esc(e.name)}</a><span class="sz">${fmtBytes(e.size)}</span></li>`;
    });
    return `<ul>${items.join("")}</ul>`;
  }

  function bindTree(root, repo) {
    $$(".dir", root).forEach((d) => {
      if (d.dataset.bound) return;
      d.dataset.bound = "1";
      d.addEventListener("click", async () => {
        const ul = d.nextElementSibling;
        if (!ul) return;
        if (!ul.hidden) { ul.hidden = true; d.classList.remove("open"); return; }
        d.classList.add("open");
        ul.hidden = false;
        if (ul.dataset.loaded) return;
        ul.innerHTML = "<li>loading…</li>";
        try {
          const data = await api(`/api/tree?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(d.dataset.path)}`);
          ul.innerHTML = renderTree(data.entries || [], repo, d.dataset.path);
          ul.dataset.loaded = "1";
          bindTree(ul, repo);
        } catch (e) {
          ul.innerHTML = `<li>error: ${esc(e.message)}</li>`;
        }
      });
    });
  }

  // ---------------------------------------------------------------- file upload state
  const fileState = { mode: "url", file: null, uploadedUrl: null, uploading: false };

  function initInputMode() {
    // Split view: Link (left) and File (right) are both visible, no toggle.
    // Typing a URL clears a selected file so exactly one source is active.
    const urlInput = $('input[name="url"]', $("#run-form"));
    if (urlInput && !urlInput.dataset.bound) {
      urlInput.dataset.bound = "1";
      urlInput.addEventListener("input", () => {
        if (urlInput.value.trim() && window.__apk2sourceClearFile) window.__apk2sourceClearFile();
      });
    }
    const btns = $$(".mode-btn");
    if (!btns.length) return;
    btns.forEach((b) => b.addEventListener("click", () => {
      btns.forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      fileState.mode = b.dataset.mode;
      const urlMode = $("#mode-url");
      const fileMode = $("#mode-file");
      const urlInput = $('input[name="url"]', $("#run-form"));
      const gameInput = $('input[name="game_name"]', $("#run-form"));
      const gameHint = gameInput?.nextElementSibling;

      if (fileState.mode === "url") {
        urlMode.hidden = false;
        fileMode.hidden = true;
        urlInput.setAttribute("required", "");
        if (gameHint) gameHint.textContent = "Used as the folder name in the destination repos.";
        urlInput.focus();
      } else {
        urlMode.hidden = true;
        fileMode.hidden = false;
        urlInput.removeAttribute("required");
        urlInput.value = ""; // clear URL — not needed in file mode
        if (gameHint) gameHint.textContent = "Auto-filled from filename (editable).";
        // Don't clear game_name if already filled
        if (!gameInput.value) {
          gameInput.focus();
        }
      }
    }));
  }

  function initFileDrop() {
    const zone = $("#drop-zone");
    const input = $("#apk-file");
    const selected = $("#drop-selected");
    const fileName = $("#drop-file-name");
    const fileSize = $("#drop-file-size");
    const clearBtn = $("#drop-clear");
    const progress = $("#upload-progress");
    const progressFill = $("#progress-fill");
    const progressText = $("#progress-text");

    if (!zone) return;

    // NOTE: #drop-zone is a <label for file input>, so a native click already
    // opens the file chooser. Do NOT call input.click() here — browsers block
    // programmatic file dialogs without a trusted user activation.
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("dragover"); });
    zone.addEventListener("dragleave", () => { zone.classList.remove("dragover"); });
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("dragover");
      const file = e.dataTransfer.files[0];
      if (file) setFile(file);
    });

    input.addEventListener("change", () => {
      if (input.files[0]) setFile(input.files[0]);
    });

    clearBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      clearFile();
    });
    window.__apk2sourceClearFile = clearFile;

    function setFile(file) {
      fileState.file = file;
      fileState.uploadedUrl = null;
      fileName.textContent = file.name;
      fileSize.textContent = fmtBytes(file.size);
      selected.hidden = false;
      zone.querySelector(".drop-content").hidden = true;
      zone.classList.add("has-file");

      // Auto-fill game name from filename (strip extension)
      const gameInput = $('input[name="game_name"]', $("#run-form"));
      if (gameInput) {
        const baseName = file.name.replace(/\.[^/.]+$/, ""); // remove extension
        const slug = baseName.replace(/[^A-Za-z0-9._-]/g, "-").replace(/-{2,}/g, "-").slice(0, 80);
        gameInput.value = slug;
        gameInput.classList.remove("auto-filled");
        void gameInput.offsetWidth; // trigger reflow
        gameInput.classList.add("auto-filled");
      }

      // Hide URL field when file is selected
      const urlMode = $("#mode-url");
      const urlInput = $('input[name="url"]', $("#run-form"));
      if (urlMode) urlMode.hidden = true;
      if (urlInput) { urlInput.removeAttribute("required"); urlInput.value = ""; }

      // Switch toggle to File
      $$(".mode-btn").forEach((b) => b.classList.remove("active"));
      const fileBtn = $('.mode-btn[data-mode="file"]');
      if (fileBtn) fileBtn.classList.add("active");
      fileState.mode = "file";

      // Check if this file matches a pending upload — update progress display
      const LS_UPLOAD = "apk2source.upload";
      try {
        const raw = localStorage.getItem(LS_UPLOAD);
        if (raw) {
          const saved = JSON.parse(raw);
          if (saved && saved.filename === file.name && saved.file_size === file.size) {
            const totalChunks = Math.ceil(file.size / (8 * 1024 * 1024));
            const completed = saved.completed_parts ? saved.completed_parts.length : 0;
            const pct = totalChunks > 0 ? (completed / totalChunks) * 85 : 0;
            setProgress(pct, `Ready to resume: ${completed}/${totalChunks} chunks done`);
          }
        }
      } catch {}
    }

    function clearFile() {
      fileState.file = null;
      fileState.uploadedUrl = null;
      input.value = "";
      selected.hidden = true;
      zone.querySelector(".drop-content").hidden = false;
      zone.classList.remove("has-file");
      progress.hidden = true;

      // Restore URL field
      const urlMode = $("#mode-url");
      const urlInput = $('input[name="url"]', $("#run-form"));
      if (urlMode) urlMode.hidden = false;
      if (urlInput) urlInput.setAttribute("required", "");

      // Switch toggle back to Link
      $$(".mode-btn").forEach((b) => b.classList.remove("active"));
      const linkBtn = $('.mode-btn[data-mode="url"]');
      if (linkBtn) linkBtn.classList.add("active");
      fileState.mode = "url";
    }

    function setProgress(pct, text) {
      progress.hidden = false;
      progressFill.style.width = `${pct}%`;
      progressText.textContent = text || `Uploading… ${Math.round(pct)}%`;
    }

    function hideProgress() {
      progress.hidden = true;
      progressFill.style.width = "0%";
    }

    // Expose for submit handler
    window.__apk2sourceFileUpload = {
      upload: async () => {
        if (!fileState.file) throw new Error("no file selected");
        if (fileState.uploadedUrl) return fileState.uploadedUrl;
        if (!state.backendOk) throw new Error("backend offline — file upload requires backend");

        const file = fileState.file;
        const gameName = $('input[name="game_name"]', $("#run-form")).value || "uploaded-game";
        const CHUNK_SIZE = 8 * 1024 * 1024; // 8MB chunks: survives flaky networks, >= R2 5MB part minimum
        const LS_UPLOAD = "apk2source.upload";

        // POST one chunk with retries (survives ERR_CONNECTION_RESET).
        const postChunk = (key, uploadId, partNumber, chunk) => {
          const sendOnce = () => new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open("POST", `${state.backend.replace(/\/$/, "")}/api/upload-chunk`);
            if (state.key) xhr.setRequestHeader("X-Panel-Key", state.key);
            xhr.timeout = 120000;

            // Overall bar only — no per-chunk text (see updateParallelProgress).
            xhr.upload.addEventListener("progress", (e) => {
              if (e.lengthComputable) {
                const doneBytes = Math.min((partNumber - 1) * CHUNK_SIZE + e.loaded, file.size);
                progressFill.style.width = `${5 + (doneBytes / file.size) * 85}%`;
              }
            });

            xhr.addEventListener("load", () => {
              try {
                const data = JSON.parse(xhr.responseText);
                if (xhr.status >= 200 && xhr.status < 300 && data.ok) resolve(data);
                else reject(new Error(data.error || `HTTP ${xhr.status}`));
              } catch { reject(new Error(`Chunk upload failed: HTTP ${xhr.status}`)); }
            });
            xhr.addEventListener("error", () => reject(new Error("Chunk upload failed — network error")));
            xhr.addEventListener("timeout", () => reject(new Error("Chunk upload timed out")));
            xhr.addEventListener("abort", () => reject(new Error("Upload aborted")));

            const formData = new FormData();
            formData.append("key", key);
            formData.append("upload_id", uploadId);
            formData.append("part_number", String(partNumber));
            formData.append("chunk", chunk, file.name);
            xhr.send(formData);
          });

          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          return (async () => {
            let lastErr = null;
            for (let attempt = 1; attempt <= 4; attempt++) {
              try {
                return await sendOnce();
              } catch (e) {
                lastErr = e;
                if (/abort/i.test(e.message)) throw e;
                if (attempt < 4) await sleep(1000 * attempt);
              }
            }
            throw lastErr;
          })();
        };

        setProgress(0, "Preparing upload…");
        fileState.uploading = true;

        // Check for resumable upload in localStorage
        let saved = null;
        try {
          const raw = localStorage.getItem(LS_UPLOAD);
          if (raw) saved = JSON.parse(raw);
        } catch {}

        const canResume = saved
          && saved.filename === file.name
          && saved.file_size === file.size
          && saved.game_name === gameName
          && saved.upload_id
          && saved.key
          && saved.completed_parts
          && saved.completed_parts.length > 0
          && saved.backend === state.backend;

        try {
          // Chunked R2 multipart upload (resumable). Single-shot POSTs die
          // with ERR_CONNECTION_RESET on big files — small chunks + retries.
          const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
          let uploadId, key, parts;

          if (canResume) {
            uploadId = saved.upload_id;
            key = saved.key;
            parts = saved.completed_parts;
            const resumePct = (parts.length / totalChunks) * 85;
            setProgress(resumePct, `Resuming from chunk ${parts.length}/${totalChunks}…`);
          } else {
            setProgress(2, `Splitting into ${totalChunks} chunks…`);
            const initResp = await api("/api/upload-url", {
              method: "POST",
              body: {
                filename: file.name,
                game_name: gameName,
                content_type: file.type || "application/octet-stream",
                file_size: file.size,
              }
            });
            if (!initResp || !initResp.ok) throw new Error((initResp && initResp.error) || "failed to initiate upload");
            uploadId = initResp.upload_id;
            key = initResp.key;
            parts = [];
          }

          // Upload remaining chunks IN PARALLEL (bounded pool).
          // R2 accepts parts in any order; we sort by partNumber on finish.
          const CONCURRENCY = 6;
          const saveProgress = () => {
            try {
              const done = [];
              for (let k = 0; k < totalChunks; k++) {
                if (slotResults[k]) done.push(slotResults[k]);
              }
              done.sort((a, b) => a.part_number - b.part_number);
              localStorage.setItem(LS_UPLOAD, JSON.stringify({
                filename: file.name,
                file_size: file.size,
                game_name: gameName,
                upload_id: uploadId,
                key: key,
                completed_parts: done,
                backend: state.backend,
                saved_at: Date.now(),
              }));
            } catch {}
          };

          // Pre-fill slots with already-uploaded parts (resume case).
          const slotResults = new Array(totalChunks).fill(null);
          for (const p of parts) {
            const idx = (typeof p.part_number === "number" ? p.part_number : parseInt(p.part_number, 10)) - 1;
            if (idx >= 0 && idx < totalChunks) slotResults[idx] = p;
          }
          let doneCount = slotResults.filter(Boolean).length;
          const queue = [];
          for (let i = 0; i < totalChunks; i++) {
            if (!slotResults[i]) queue.push(i);
          }

          const updateParallelProgress = () => {
            const pct = 5 + (doneCount / totalChunks) * 85;
            const doneBytes = Math.min(doneCount * CHUNK_SIZE, file.size);
            setProgress(pct, `Uploading… ${Math.round(pct)}% · ${fmtBytes(doneBytes)} / ${fmtBytes(file.size)}`);
          };
          updateParallelProgress();

          const parallelWorker = async () => {
            while (queue.length) {
              if (!fileState.uploading) throw new Error("Upload aborted");
              const i = queue.shift();
              const start = i * CHUNK_SIZE;
              const end = Math.min(start + CHUNK_SIZE, file.size);
              const chunk = file.slice(start, end);
              const partNumber = i + 1;
              const partResult = await postChunk(key, uploadId, partNumber, chunk);
              slotResults[i] = { part_number: partResult.part_number, etag: partResult.etag };
              doneCount++;
              saveProgress();
              updateParallelProgress();
            }
          };

          const laneCount = Math.min(CONCURRENCY, queue.length);
          const lanes = [];
          for (let l = 0; l < laneCount; l++) lanes.push(parallelWorker());
          await Promise.all(lanes);

          parts = slotResults.filter(Boolean);
          parts.sort((a, b) => a.part_number - b.part_number);
          if (parts.length !== totalChunks) {
            throw new Error(`upload incomplete: ${parts.length}/${totalChunks} chunks done`);
          }

          // Complete multipart upload
          setProgress(92, "Finalizing upload…");
          localStorage.removeItem(LS_UPLOAD);

          const finishResp = await api("/api/upload-finish", {
            method: "POST",
            body: { key, upload_id: uploadId, parts }
          });
          if (!finishResp || !finishResp.ok) throw new Error((finishResp && finishResp.error) || "failed to finalize upload");

          const downloadUrl = `${state.backend.replace(/\/$/, "")}/api/download?key=${encodeURIComponent(key)}`;

          setProgress(100, "Upload complete!");
          localStorage.removeItem(LS_UPLOAD);
          fileState.uploadedUrl = downloadUrl;
          setTimeout(() => { progress.hidden = true; }, 1500);
          return downloadUrl;
        } finally {
          fileState.uploading = false;
        }
      }
    };
  }

  // ---------------------------------------------------------------- dispatch
  function initRunForm() {
    const form = $("#run-form");
    const msg = $("#run-msg");
    const btn = $("#btn-run");
    const liveNew = $("#live-new");
    if (liveNew && !liveNew.dataset.bound) {
      liveNew.dataset.bound = "1";
      liveNew.addEventListener("click", () => {
        stopLive();
        const card = $("#live");
        if (card) card.hidden = true;
        if (form) form.hidden = false;
        setMsg(msg, "", "");
      });
    }

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(form);

      const hasFile = fileState.file && fileState.file.size > 0;
      const hasUrl = !!(fd.get("url") || "").trim();
      if (!hasFile && !hasUrl) {
        setMsg(msg, "Provide a Payload URL or select a file.", "err");
        return;
      }

      // Game name is auto-generated (no visible field): from filename or URL.
      const slugify = (s) => String(s || "").replace(/\.[^/.]+$/, "")
        .replace(/[^A-Za-z0-9._-]/g, "-").replace(/-{2,}/g, "-")
        .replace(/^[-.]+|[-.]+$/g, "").slice(0, 80) || "game";
      let gameName = (fd.get("game_name") || "").trim();
      if (!gameName) {
        if (hasFile) {
          gameName = slugify(fileState.file.name);
        } else {
          try {
            const u = new URL(fd.get("url").trim());
            const base = u.pathname.split("/").filter(Boolean).pop() || "game";
            gameName = slugify(decodeURIComponent(base));
          } catch { gameName = `game-${Date.now().toString(36)}`; }
        }
      }

      btn.disabled = true;

      // --- FILE MODE ---
      // Small files: single POST to /api/run (Worker saves to R2 + dispatches).
      // Large files: chunked R2 multipart upload (page must stay open during
      // upload, refresh+resume works), then dispatch via /api/trigger.
      // After dispatch the pipeline runs on GitHub Actions — page can close.
      if (hasFile) {
        const SINGLE_SHOT_LIMIT = 10 * 1024 * 1024; // one POST only for tiny files; bigger files go chunked
        const buildCommonInputs = (url) => {
          const inputs = { url, game_name: gameName };
          if (fd.get("url_fallback")) inputs.url_fallback = fd.get("url_fallback");
          if (fd.get("sha256")) inputs.sha256 = fd.get("sha256");
          // All stages on by default (no switches in UI anymore).
          inputs.use_cache = "true";
          inputs.run_device_cache = "true";
          inputs.run_java_decompile = "true";
          inputs.run_il2cpp = "true";
          inputs.run_assetripper = "true";
          inputs.publish_spine = "true";
          inputs.publish_game_source = "true";
          inputs.runner = "ubuntu-latest";
          inputs.spine_repo = "vladleopold/source_spine";
          inputs.source_repo = "vladleopold/game_source";
          inputs.max_texture_side = "0";
          inputs.game_name = gameName;
          return inputs;
        };

        if (fileState.file.size <= SINGLE_SHOT_LIMIT) {
          setMsg(msg, "Uploading & dispatching…", "");
          try {
            const runFd = new FormData();
            runFd.append("file", fileState.file);
            runFd.append("game_name", gameName);
            if (fd.get("url_fallback")) runFd.append("url_fallback", fd.get("url_fallback"));
            if (fd.get("sha256")) runFd.append("sha256", fd.get("sha256"));
            runFd.append("use_cache", "true");
            runFd.append("run_device_cache", "true");
            runFd.append("run_java_decompile", "true");
            runFd.append("run_il2cpp", "true");
            runFd.append("run_assetripper", "true");
            runFd.append("publish_spine", "true");
            runFd.append("publish_game_source", "true");
            runFd.append("runner", fd.get("runner") || "ubuntu-latest");
            runFd.append("spine_repo", fd.get("spine_repo") || "leaopold/source_spine");
            runFd.append("source_repo", fd.get("source_repo") || "leaopold/game_source");
            runFd.append("max_texture_side", fd.get("max_texture_side") || "0");

            const result = await new Promise((resolve, reject) => {
              const xhr = new XMLHttpRequest();
              xhr.open("POST", `${state.backend.replace(/\/$/, "")}/api/run`);
              if (state.key) xhr.setRequestHeader("X-Panel-Key", state.key);

              xhr.upload.addEventListener("progress", (e) => {
                if (e.lengthComputable) {
                  const pct = Math.round((e.loaded / e.total) * 100);
                  setMsg(msg, `Uploading ${fmtBytes(e.loaded)} / ${fmtBytes(e.total)} (${pct}%)…`, "");
                }
              });

              xhr.addEventListener("load", () => {
                try {
                  const data = JSON.parse(xhr.responseText);
                  if (xhr.status >= 200 && xhr.status < 300 && data.ok) resolve(data);
                  else reject(new Error(data.error || `HTTP ${xhr.status}`));
                } catch { reject(new Error(`Request failed: HTTP ${xhr.status}`)); }
              });
              xhr.addEventListener("error", () => reject(new Error("Network error")));
              xhr.addEventListener("abort", () => reject(new Error("Aborted")));
              xhr.send(runFd);
            });

            // Clear upload state
            localStorage.removeItem("apk2source.upload");
            fileState.file = null;
            fileState.uploadedUrl = null;

            const runLink = result.run_url ? `<a href="${esc(result.run_url)}" target="_blank">Open pipeline →</a>` : "";
            setMsg(msg, `✓ Done! Pipeline dispatched. You can close this page. ${runLink}`, "ok");
            setTimeout(loadRuns, 8000);
            startLive(result.run_id, result.run_url, gameName);
          } catch (e) {
            setMsg(msg, `Error: ${e.message}`, "err");
          } finally {
            btn.disabled = false;
          }
          return;
        }

        // Large file: chunked upload first (keep page open; refresh resumes),
        // then dispatch — after dispatch the page can be closed.
        setMsg(msg, "Uploading large file in chunks — keep this page open (refresh resumes)…", "");
        try {
          const downloadUrl = await window.__apk2sourceFileUpload.upload();
          setMsg(msg, "Upload complete. Dispatching pipeline…", "ok");
          const inputs = buildCommonInputs(downloadUrl);
          const r = await api("/api/trigger", { method: "POST", body: { workflow: "pipeline.yml", inputs } });
          localStorage.removeItem("apk2source.upload");
          fileState.file = null;
          fileState.uploadedUrl = null;
          const runLink = r.run_url ? `<a href="${esc(r.run_url)}" target="_blank">Open pipeline →</a>` : "";
          setMsg(msg, `✓ Done! Pipeline dispatched. You can close this page. ${runLink}`, "ok");
          setTimeout(loadRuns, 8000);
          startLive(r.run_id, r.run_url, gameName);
        } catch (e) {
          setMsg(msg, `Error: ${e.message}`, "err");
        } finally {
          btn.disabled = false;
        }
        return;
      }

      // --- URL MODE: dispatch pipeline directly (all stages on) ---
      const inputs = {
        url: (fd.get("url") || "").trim(),
        game_name: gameName,
        use_cache: "true",
        run_device_cache: "true",
        run_java_decompile: "true",
        run_il2cpp: "true",
        run_assetripper: "true",
        publish_spine: "true",
        publish_game_source: "true",
        runner: "ubuntu-latest",
        spine_repo: "vladleopold/source_spine",
        source_repo: "vladleopold/game_source",
        max_texture_side: "0",
      };

      setMsg(msg, "Dispatching pipeline…");
      try {
        if (state.backendOk) {
          const r = await api("/api/trigger", { method: "POST", body: { workflow: "pipeline.yml", inputs } });
          setMsg(msg, `Dispatched. ${r.run_url ? `<a href="${esc(r.run_url)}" target="_blank">Open pipeline →</a>` : "Check History tab."}`, "ok");
          setTimeout(loadRuns, 8000);
          startLive(r.run_id, r.run_url, inputs.game_name);
        } else {
          const url = `https://github.com/${REPO}/actions/workflows/pipeline.yml`;
          window.open(url, "_blank");
          setMsg(msg, "Opened GitHub Actions. Fill the form and press Run.", "ok");
        }
      } catch (e) {
        setMsg(msg, `Error: ${e.message}`, "err");
      } finally {
        btn.disabled = false;
      }
    });
  }

  // ---------------------------------------------------------------- settings
  function initSettings() {
    const modal = $("#settings");
    $("#btn-settings").addEventListener("click", () => {
      $("#set-backend").value = state.backend || "";
      $("#set-key").value = state.key || "";
      modal.hidden = false;
      $("#set-backend").focus();
    });
    $("#set-close").addEventListener("click", () => { modal.hidden = true; });
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.hidden = true; });
    $("#set-save").addEventListener("click", async () => {
      state.backend = $("#set-backend").value.trim();
      state.key = $("#set-key").value.trim();
      if (state.backend) localStorage.setItem(LS_BACKEND, state.backend); else localStorage.removeItem(LS_BACKEND);
      if (state.key) localStorage.setItem(LS_KEY, state.key); else localStorage.removeItem(LS_KEY);
      modal.hidden = true;
      await detectBackend();
      loadRuns();
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") modal.hidden = true; });
  }

  // ---------------------------------------------------------------- refresh
  function initRefresh() {
    $("#btn-refresh").addEventListener("click", loadRuns);
    $("#btn-spine-reload").addEventListener("click", loadSpine);
    const tick = () => {
      clearInterval(state.timer);
      if ($("#auto-refresh").checked) state.timer = setInterval(loadRuns, 30000);
    };
    $("#auto-refresh").addEventListener("change", tick);
    tick();
  }

  // ---------------------------------------------------------------- restore pending upload
  function restorePendingUpload() {
    const LS_UPLOAD = "apk2source.upload";
    try {
      const raw = localStorage.getItem(LS_UPLOAD);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (!saved || !saved.filename || !saved.upload_id || !saved.key) return;

      // Show pending upload info
      const dropZone = $("#drop-zone");
      const selected = $("#drop-selected");
      const fileName = $("#drop-file-name");
      const fileSize = $("#drop-file-size");
      const progress = $("#upload-progress");
      const progressFill = $("#progress-fill");
      const progressText = $("#progress-text");
      const gameInput = $('input[name="game_name"]', $("#run-form"));
      const urlMode = $("#mode-url");
      const urlInput = $('input[name="url"]', $("#run-form"));

      if (dropZone && selected && fileName) {
        fileName.textContent = saved.filename;
        if (fileSize) fileSize.textContent = fmtBytes(saved.file_size || 0);
        selected.hidden = false;
        dropZone.querySelector(".drop-content").hidden = true;
        dropZone.classList.add("has-file");

        // Show progress
        const totalChunks = Math.ceil((saved.file_size || 0) / (8 * 1024 * 1024));
        const completed = saved.completed_parts ? saved.completed_parts.length : 0;
        const pct = totalChunks > 0 ? (completed / totalChunks) * 100 : 0;
        if (progress) progress.hidden = false;
        if (progressFill) progressFill.style.width = `${pct}%`;
        if (progressText) progressText.textContent = `Pending: ${completed}/${totalChunks} chunks uploaded — select the same file to resume`;

        // Auto-fill game name
        if (gameInput && saved.game_name) gameInput.value = saved.game_name;

        // Switch to file mode
        if (urlMode) urlMode.hidden = true;
        if (urlInput) { urlInput.removeAttribute("required"); urlInput.value = ""; }
        $$(".mode-btn").forEach((b) => b.classList.remove("active"));
        const fileBtn = $('.mode-btn[data-mode="file"]');
        if (fileBtn) fileBtn.classList.add("active");
        fileState.mode = "file";
      }
    } catch {}
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    initTabs();
    initSettings();
    initInputMode();
    initFileDrop();
    initRunForm();
    restorePendingUpload();
    renderDag({});
    await loadConfig();
    await detectBackend();
    initRefresh();
    await loadRuns();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
