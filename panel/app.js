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
    $("#cfg-spine-repo").textContent = state.config.spine_repo || "source_spine";
    $("#cfg-source-repo").textContent = state.config.source_repo || "game_source";
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
    const dag = $("#dag");
    if (!dag) return;
    const levels = [...new Set(STAGES.map((s) => s.level))].sort();
    dag.innerHTML = levels.map((lv) => {
      const nodes = STAGES.filter((s) => s.level === lv);
      return `<div class="dag-level">
        <div class="lvl-title">level ${lv}${lv === 2 ? " · parallel" : ""}</div>
        ${nodes.map((n) => {
          const st = jobStates[n.id];
          const stCls = st ? `st-${st}` : "st-wait";
          const stTxt = st || "idle";
          return `<div class="node ${n.cls}" data-stage="${n.id}">
            <span class="st ${stCls}">${esc(stTxt)}</span>
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

  function jobStatesFromRun(run) {
    const out = {};
    if (!run || !run.jobs) return out;
    for (const j of run.jobs) {
      const key = (j.name || "").toLowerCase();
      const match = STAGES.find((s) => key.includes(s.id) || key.includes(s.name.split(" ").slice(-1)[0]));
      if (!match) continue;
      out[match.id] = j.status === "completed"
        ? (j.conclusion === "success" ? "ok" : j.conclusion === "skipped" ? "skip" : "fail")
        : (j.status === "in_progress" ? "run" : "wait");
    }
    return out;
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

    $$("[data-run]", tbody).forEach((b) => b.addEventListener("click", () => showRun(b.dataset.run)));
    renderLatest(runs[0]);
  }

  function renderLatest(run) {
    const el = $("#latest-run");
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
    const btns = $$(".mode-btn");
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

    zone.addEventListener("click", (e) => {
      if (e.target === clearBtn || clearBtn.contains(e.target)) return;
      input.click();
    });

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
      e.stopPropagation();
      clearFile();
    });

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
        const SMALL_FILE_LIMIT = 100 * 1024 * 1024; // 100MB
        const CHUNK_SIZE = 80 * 1024 * 1024; // 80MB chunks (safe under Worker limit)

        setProgress(0, "Preparing upload…");
        fileState.uploading = true;

        try {
          let downloadUrl;

          if (file.size <= SMALL_FILE_LIMIT) {
            // Small file: upload via Worker to GitHub Release
            setProgress(5, "Uploading to GitHub…");
            const form = new FormData();
            form.append("file", file);
            form.append("game_name", gameName);

            const result = await new Promise((resolve, reject) => {
              const xhr = new XMLHttpRequest();
              xhr.open("POST", `${state.backend.replace(/\/$/, "")}/api/upload`);
              if (state.key) xhr.setRequestHeader("X-Panel-Key", state.key);

              xhr.upload.addEventListener("progress", (e) => {
                if (e.lengthComputable) {
                  setProgress(5 + (e.loaded / e.total) * 85, `Uploading… ${fmtBytes(e.loaded)} / ${fmtBytes(e.total)}`);
                }
              });

              xhr.addEventListener("load", () => {
                try {
                  const data = JSON.parse(xhr.responseText);
                  if (xhr.status >= 200 && xhr.status < 300 && data.ok) resolve(data);
                  else reject(new Error(data.error || `HTTP ${xhr.status}`));
                } catch { reject(new Error(`Upload failed: HTTP ${xhr.status}`)); }
              });
              xhr.addEventListener("error", () => reject(new Error("Upload failed — network error")));
              xhr.addEventListener("abort", () => reject(new Error("Upload aborted")));
              xhr.send(form);
            });
            downloadUrl = result.url;

          } else {
            // Large file: chunked R2 multipart upload
            const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
            setProgress(2, `Splitting into ${totalChunks} chunks…`);

            // Step 1: Initiate multipart upload
            const initResp = await api("/api/upload-url", {
              method: "POST",
              body: {
                filename: file.name,
                game_name: gameName,
                content_type: file.type || "application/octet-stream",
                file_size: file.size,
              }
            });
            if (!initResp.ok) throw new Error(initResp.error || "failed to initiate upload");

            const { upload_id: uploadId, key } = initResp;
            const parts = [];

            // Step 2: Upload chunks
            for (let i = 0; i < totalChunks; i++) {
              const start = i * CHUNK_SIZE;
              const end = Math.min(start + CHUNK_SIZE, file.size);
              const chunk = file.slice(start, end);
              const partNumber = i + 1;

              setProgress(5 + (i / totalChunks) * 85, `Uploading chunk ${partNumber}/${totalChunks}… ${fmtBytes(start)}-${fmtBytes(end)} / ${fmtBytes(file.size)}`);

              const formData = new FormData();
              formData.append("key", key);
              formData.append("upload_id", uploadId);
              formData.append("part_number", String(partNumber));
              formData.append("chunk", chunk, file.name);

              const partResult = await new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                xhr.open("POST", `${state.backend.replace(/\/$/, "")}/api/upload-chunk`);
                if (state.key) xhr.setRequestHeader("X-Panel-Key", state.key);

                xhr.upload.addEventListener("progress", (e) => {
                  if (e.lengthComputable) {
                    const chunkPct = (e.loaded / e.total) * (85 / totalChunks);
                    const basePct = 5 + (i / totalChunks) * 85;
                    setProgress(basePct + chunkPct, `Chunk ${partNumber}/${totalChunks}: ${fmtBytes(e.loaded)} / ${fmtBytes(e.total)}`);
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
                xhr.addEventListener("abort", () => reject(new Error("Upload aborted")));
                xhr.send(formData);
              });

              parts.push({ part_number: partResult.part_number, etag: partResult.etag });
            }

            // Step 3: Complete multipart upload
            setProgress(92, "Finalizing upload…");
            const finishResp = await api("/api/upload-finish", {
              method: "POST",
              body: { key, upload_id: uploadId, parts }
            });
            if (!finishResp.ok) throw new Error(finishResp.error || "failed to finalize upload");

            // Download URL goes through the Worker's download proxy
            downloadUrl = `${state.backend.replace(/\/$/, "")}/api/download?key=${encodeURIComponent(key)}`;
          }

          setProgress(100, "Upload complete!");
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

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(form);
      const inputs = {};
      for (const [k, v] of fd.entries()) {
        if (v === "") continue;
        inputs[k] = v;
      }
      $$('input[type=checkbox]', form).forEach((c) => { inputs[c.name] = c.checked ? "true" : "false"; });

      // Validate game_name is always required
      if (!inputs.game_name) {
        setMsg(msg, "Game name is required.", "err");
        return;
      }

      // If a file is selected (regardless of mode toggle), upload it
      if (fileState.file) {
        if (!fileState.uploadedUrl) {
          btn.disabled = true;
          setMsg(msg, "Uploading file…", "");
          try {
            const url = await window.__apk2sourceFileUpload.upload();
            inputs.url = url;
            setMsg(msg, "Upload complete. Dispatching pipeline…", "ok");
          } catch (e) {
            setMsg(msg, `Upload failed: ${e.message}`, "err");
            btn.disabled = false;
            return;
          }
        } else {
          inputs.url = fileState.uploadedUrl;
        }
      } else if (!inputs.url) {
        // No file, no URL
        setMsg(msg, "Provide a Payload URL or select a file to upload.", "err");
        return;
      }

      btn.disabled = true;
      setMsg(msg, "Opening GitHub Actions…");
      try {
        if (state.backendOk) {
          const r = await api("/api/trigger", { method: "POST", body: { workflow: "pipeline.yml", inputs } });
          setMsg(msg, `Dispatched. ${r.run_url ? `Tracking ${r.run_url}` : "Check the History tab in ~10s."}`, "ok");
          setTimeout(loadRuns, 8000);
        } else {
          const url = `https://github.com/${REPO}/actions/workflows/pipeline.yml`;
          const win = window.open(url, "_blank");
          if (!win) {
            setMsg(msg, "Pop-up blocked! Allow pop-ups for this site.", "err");
          } else {
            setMsg(msg, `Opened in new tab. Click "Run workflow" below the title, fill the form, and press "Run workflow".`, "ok");
          }
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

  // ---------------------------------------------------------------- boot
  async function boot() {
    initTabs();
    initSettings();
    initInputMode();
    initFileDrop();
    initRunForm();
    renderDag({});
    await loadConfig();
    await detectBackend();
    initRefresh();
    await loadRuns();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
