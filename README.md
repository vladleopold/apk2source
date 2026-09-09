# apk2source

**Automated reverse-engineering & asset-extraction pipeline for Android builds.**
`.apk / .apks / .xapk / .apkm / .aab` → merged APK tree → engine fingerprint →
Unity asset decompilation → **Spine `json` / `atlas` / `png`** → published to
`source_spine` and `game_source`.

Everything runs on **GitHub Actions**. Nothing runs on your machine.
Control surface: a **GitHub Pages** panel backed by a **Vercel** serverless proxy.

---

## 0. What this process is actually called

You asked what the whole thing is named. There is no single trademarked term; in
industry it is a **reverse-engineering pipeline (RE pipeline)**, and more
precisely a combination of three named disciplines:

| Term | What it covers here |
|---|---|
| **Reverse engineering (RE)** | Recovering design/architecture from a shipped binary |
| **Static analysis** | Parsing the APK/AAB, DEX, IL2CPP metadata, Unity containers without executing |
| **Dynamic analysis** | Installing on an emulator, first-run, capturing what the app downloads at runtime |
| **Decompilation** | DEX → Java (jadx), resources.arsc → XML (apktool), IL2CPP → C# signatures (Il2CppDumper) |
| **Asset extraction / "ripping"** | Unity containers → textures, text assets, meshes, audio |
| **Binary-to-source reconstruction** | AssetRipper → an openable Unity project (`game_source`) |
| **Asset pipeline / build combine** | The multi-stage CI/CD orchestration itself |

The closest common shop-floor name for the whole machine is a
**"decompilation / asset-extraction combine"** or **RE automation pipeline**.
That is what this repo is.

---

## 1. Master development plan

> This section is the structured version of the original request. It is the
> contract the implementation follows; checkboxes reflect real state.

### Phase 0 — Foundations
- [x] **0.1** Repository `apk2source` (public) + GitHub Pages + CI/CD.
- [x] **0.2** Destination repositories `source_spine`, `game_source`.
- [x] **0.3** Local working copy at `/Volumes/Work/apk2source` (not `/tmp`).
- [x] **0.4** Python toolkit packaged (`tools/apk2source`), installable via `pip install -e ./tools`.
- [x] **0.5** Zero-heavy-dependency policy: `UnityPy` + `Pillow` only; everything else is stdlib.

### Phase 1 — Test harness (built first, on purpose)
- [x] **1.1** Fixture generator that emits **real** Unity binary containers from
      scratch: SerializedFile (`.assets`), UnityFS (`.bundle`), UnityFS+LZ4.
- [x] **1.2** Fixture carries a genuine Spine payload: skeleton `.json`,
      skeleton `.skel` (real Spine binary header), `.atlas`, `Texture2D` pages,
      plus the spine-unity `MonoBehaviour`/`MonoScript` graph and an
      `AssetBundle` `m_Container`.
- [x] **1.3** Synthetic `.apks` split set (base + config + asset pack) with a
      parseable binary `AndroidManifest.xml`.
- [x] **1.4** `apk2source selftest` — full chain + **byte-exact** verification.
      Current result: **21/21 checks, 12/12 byte-exact assets.**
- [x] **1.5** 35 pytest unit tests across containers / spine / unity / bus / gitpub / report.

### Phase 2 — Stage 01 ACQUIRE
- [x] **2.1** Resumable HTTP download (`Range`), retries with backoff, mirrors.
- [x] **2.2** sha256 verification + `.acquire.json` sidecar record.
- [x] **2.3** Two-level cache: on-disk cache dir (with `*.meta.json` records) and
      `actions/cache` keyed on `game-slug + sha256|url-hash`.
- [x] **2.4** `--no-cache` switch, exposed in the panel as "Use payload cache".
- [x] **2.5** `file://` support so the selftest exercises the same code path.

### Phase 3 — Stage 02 UNPACK
- [x] **3.1** Container detection: `.apks` (SAI/bundletool), `.xapk`, `.apkm`,
      `.aab`, plain `.apk`, unknown zip — by **content**, not extension.
- [x] **3.2** Split classification: `base` / `config` (dpi, locale, ABI) / `asset_pack` (`ggpack*`).
- [x] **3.3** Merge into one tree (base → configs → packs, later wins), skipping
      per-split signature files.
- [x] **3.4** Binary AXML string-pool parser → package id recovery (no `aapt2` needed).
- [x] **3.5** Zip-slip guard on every extraction.
- [x] **3.6** `merged_listing.json` (per-top-level and per-extension byte census).

### Phase 4 — Stage 02b ENGINE DETECTION
- [x] **4.1** Evidence-based fingerprinting: Unity / Unreal / Godot / libGDX /
      Cocos / Defold / Corona / native.
- [x] **4.2** Unity version probe from `globalgamemanagers`, `data.unity3d`,
      UnityFS headers and raw SerializedFiles.
- [x] **4.3** ABI list, native lib inventory, DEX count, `resources.arsc` presence.
- [x] **4.4** Spine pre-signals (`.atlas` files, spine runtime `.so`/`.dll`).

### Phase 5 — Stage 03 DEVICE CACHE CAPTURE
- [x] **5.1** `adb` plumbing: device discovery, `getprop` inventory.
- [x] **5.2** `install-multiple` for split sets with **ABI filtering** (only
      splits matching the device ABI are pushed, otherwise install fails).
- [x] **5.3** Launch via `resolve-activity` → `am start`, `monkey` fallback.
- [x] **5.4** **Snapshot-diff method** (no root required):
      `pre-install` → `post-install` → `post-first-run`.
      `cache = post_run − post_install`, `shipped = post_install − pre_install`.
- [x] **5.5** Settle loop: poll until the data-dir total is stable for N rounds
      (this is how you know the first-run download finished).
- [x] **5.6** `adb pull` of the cache set into `work/device-cache/cache`.
- [x] **5.7** Runs on `macos-latest` via `reactivecircus/android-emulator-runner`
      with AVD snapshot caching; `continue-on-error` so a device failure never
      kills the pipeline.

### Phase 6 — Stage 04 DECOMPILATION
- [x] **6.1** Unity file discovery: known paths, Unity-ish suffixes, and
      **magic-byte sniffing** for renamed/extensionless blobs.
- [x] **6.2** `UnityPy` extraction of TextAsset / Texture2D / Sprite /
      MonoBehaviour / Mesh / AudioClip / VideoClip.
- [x] **6.3** **`AssetBundle.m_Container` paths preserved as directories** — this
      is what makes Spine re-pairing possible downstream.
- [x] **6.4** Parallel extraction across files (`ProcessPoolExecutor`).
- [x] **6.5** Binary TextAsset safety: `surrogateescape` round-trip so `.skel.bytes`
      survives intact.
- [x] **6.6** Optional texture downscale cap (`--max-texture-side`).
- [x] **6.7** Self-provisioning external tools (downloaded from official GitHub
      releases into `$APK2SOURCE_TOOLS`): `apktool`, `jadx`, `AssetRipper`, `Il2CppDumper`.
- [x] **6.8** `survey` command: reports what this build can actually be
      decompiled with (`can_jadx`, `can_il2cpp`, `can_assetripper`).

### Phase 7 — Stage 05 SPINE EXTRACTION
- [x] **7.1** **Content-based** detection, never filename-based:
      - skeleton `.json` — JSON with `skeleton.spine` + `bones` + `slots`
      - skeleton `.skel` — real Spine binary header (varint strings: hash, version)
      - `.atlas` — full Spine atlas page grammar (page header → page props → regions)
      - texture pages — PNG magic
- [x] **7.2** Pairing precedence: atlas page filename → exact stem → folder-local
      → single-skeleton-folder fallback.
- [x] **7.3** **Content-hash dedupe** across bundles (the same skeleton shipped in
      3 bundles is exported once, with `duplicate_of` provenance).
- [x] **7.4** `.json` + `.skel` of the same character merged into one entry.
- [x] **7.5** Atlas page filenames rewritten to the names actually written to disk.
- [x] **7.6** Output layout:
      `source_spine/<game-slug>/<skeleton>/{<skeleton>.json, .skel.bytes, .atlas, *.png, _meta.json}`
      plus `<game-slug>/_index.json`.
- [x] **7.7** Per-skeleton `_meta.json` provenance: source path, sha256, spine
      version, bone/slot counts, animation list, completeness flag.

### Phase 8 — Stage 06 PUBLISH
- [x] **8.1** **Partial clone** (`--filter=blob:none` + sparse-checkout) so
      publishing one game never downloads a multi-GB history.
- [x] **8.2** Automatic **Git LFS** when the largest file crosses the threshold.
- [x] **8.3** `direct` and `pr` publish modes.
- [x] **8.4** Idempotent: no-change runs are a no-op, not an empty commit.
- [x] **8.5** Push retries with backoff; token injected via
      `x-access-token:` and never written to disk or logs.

### Phase 9 — Artifact bus (multi-GB transfer between workflows)
- [x] **9.1** `NullBus` / `LocalBus` / `S3Bus` behind one interface.
- [x] **9.2** **SigV4 implemented from scratch** (no boto3) — works against
      Cloudflare R2, MinIO and AWS S3; single PUT up to 5 GiB, ranged GET resume.
- [x] **9.3** Auto-enabled when `APK2SOURCE_S3_*` secrets exist; the orchestrator
      detects this in the `plan` job and reports it.
- [x] **9.4** **Without a bus** the design adapts: heavy stages share one runner,
      and parallel jobs restore the payload from `actions/cache` instead of
      moving it through artifacts (GitHub gives public repos only 1 GB of
      artifact storage — a 2.8 GB payload can never travel that way).

### Phase 10 — CI/CD workflows
- [x] **10.1** `ci.yml` — lint, 35 unit tests, and the E2E selftest across a
      **matrix of Unity versions** (2019.4 / 2021.3 / 2022.3).
- [x] **10.2** `pipeline.yml` — the orchestrator: 8 jobs, 5 levels, parallel fan-out.
- [x] **10.3** `stage.yml` — reusable + independently dispatchable single-stage
      runner (`workflow_dispatch` **and** `workflow_call`).
- [x] **10.4** `pages.yml` — bakes live run history from the GitHub API into
      `panel-data/` and deploys Pages; also on a 30-minute schedule.
- [x] **10.5** `repository_dispatch` entry point so the panel (or any external
      system) can start a run.

### Phase 11 — Control panel (GitHub Pages)
- [x] **11.1** Dependency-free vanilla SPA (no build step, no framework).
- [x] **11.2** **Run** tab — dispatch form with every pipeline switch.
- [x] **11.3** **Pipeline** tab — live DAG of the 5 levels + stage contract table.
- [x] **11.4** **History** tab — run table with 30 s auto-refresh and per-run job drill-down.
- [x] **11.5** **Spine library** tab — lazy-loading tree browser over `source_spine` / `game_source`.
- [x] **11.6** **Docs** tab + legal notice.
- [x] **11.7** Works **degraded** with no backend (static baked data only).

### Phase 12 — Backend (Vercel)
- [x] **12.1** `GET /api/health`, `/api/config`
- [x] **12.2** `GET /api/runs`, `/api/run/:id`, `/api/artifacts`, `/api/artifact-download`
- [x] **12.3** `GET /api/tree` — repo browser with an owner allow-list
- [x] **12.4** `POST /api/trigger`, `POST /api/stage` — dispatch, input whitelist,
      guarded by `PANEL_ACCESS_KEY`
- [x] **12.5** **The GitHub token lives only in Vercel env.** It never reaches the
      browser. Mutating calls need the shared panel key.

### Phase 13 — Verification
- [x] **13.1** Local: 35/35 unit tests, 21/21 selftest checks, 12/12 byte-exact.
- [x] **13.2** CI: same suite on GitHub Actions across 3 Unity versions.
- [x] **13.3** Publish path proven against a real bare git remote in tests.
- [ ] **13.4** Production run against a real licensed target — **yours to trigger.**

---

## 2. Architecture

```
                         ┌──────────────────────────────┐
   GitHub Pages panel ──▶│  Vercel backend (token vault) │
   (static SPA)          └──────────────┬───────────────┘
                                        │ workflow_dispatch / repository_dispatch
                                        ▼
┌───────────────────────────── pipeline.yml ─────────────────────────────┐
│ level 0   plan            resolve inputs · slug · cache key · bus      │
│ level 1   heavy           01 acquire → 02 unpack → 02b detect          │
│                           → 04 unity-extract → 05 spine-extract        │
│ level 2   ┌ device-cache  03 emulator install → first run → diff       │
│  (par.)   ├ java-decomp   04a apktool + jadx                           │
│           ├ il2cpp        04b Il2CppDumper                             │
│           └ unity-project 04c AssetRipper → ExportedProject            │
│ level 3   spine-publish → source_spine     source-publish → game_source│
│ level 4   report → panel-data/*.json → Pages                           │
└────────────────────────────────────────────────────────────────────────┘
        ▲                                            │
        └──── artifact bus (S3/R2) or actions/cache ─┘
```

**Why the heavy stages share one runner.** GitHub gives public repos 1 GB of
artifact storage and 10 GB of cache. A split-APK set is routinely 2–3 GB, so it
cannot travel between jobs as an artifact. The design therefore keeps the big
payload on one runner and only moves *small* things (state JSON, `base.apk`,
IL2CPP inputs, extracted Spine output) across job boundaries. When you do
configure an S3/R2 bus, the `plan` job detects it and parallel jobs may pull the
payload from there instead of re-downloading.

---

## 3. Repository layout

```
apk2source/
├── README.md                     ← this plan
├── DEVBOOK.MD                    ← full development session log
├── LICENSE                       ← MIT (the tooling)
├── docs/
│   ├── ARCHITECTURE.md           ← data flow, formats, failure modes
│   ├── PIPELINE.md               ← stage-by-stage reference
│   ├── RUNBOOK.md                ← how to operate it
│   └── LEGAL.md                  ← what you may and may not point this at
├── tools/
│   ├── pyproject.toml
│   ├── apk2source/
│   │   ├── cli.py                ← single entry point for every stage
│   │   ├── acquire.py            ← 01 download + cache
│   │   ├── containers.py         ← 02 apks/xapk/apkm/aab → merged
│   │   ├── engine.py             ← 02b engine fingerprint
│   │   ├── device.py             ← 03 emulator first-run cache capture
│   │   ├── unity_extract.py      ← 04 UnityPy → asset tree
│   │   ├── decompilers.py        ← 04a/b/c apktool·jadx·Il2CppDumper·AssetRipper
│   │   ├── spine.py              ← 05 json/skel/atlas/png discovery + export
│   │   ├── gitpub.py             ← 06 sparse clone + LFS + push
│   │   ├── bus.py                ← S3/R2 SigV4 artifact bus
│   │   ├── report.py             ← pipeline.json + Markdown summary
│   │   └── util.py               ← logging, hashing, slug, zip-slip guard
│   └── tests/                    ← 35 unit tests + the fixture generator
├── .github/workflows/
│   ├── ci.yml  pipeline.yml  stage.yml  pages.yml
├── panel/                        ← GitHub Pages control panel
│   ├── index.html  app.js  styles.css
├── panel-data/                   ← baked run history (written by CI)
└── backend/                      ← Vercel serverless proxy
    ├── vercel.json  package.json
    └── api/  health config runs run artifacts artifact-download tree trigger stage
```

---

## 4. Quick start

### Run the self-test (no target needed)
```bash
python -m pip install -e ./tools
apk2source selftest --work ./build/selftest --game demo
```
Generates a real Unity AssetBundle carrying a Spine payload, wraps it in a
synthetic `.apks`, and drives it through every stage, verifying each exported
byte against the generator's manifest.

### Run the pipeline from the panel
1. Open the Pages URL (badge below / repo → Settings → Pages).
2. **Run** tab → paste a URL you are entitled to analyse → set the game name →
   choose stages → **Dispatch pipeline**.
3. Watch the DAG and the History tab.

### Run the pipeline from the CLI
```bash
gh workflow run pipeline.yml \
  -f url='https://example.com/game.apks' \
  -f game_name='my-game' \
  -f use_cache=true \
  -f publish_spine=true
```

### Run one stage only
```bash
gh workflow run stage.yml -f stage=spine -f game_name=my-game
```

---

## 5. Required secrets

Set under **Settings → Secrets and variables → Actions**.

| Secret | Needed for | Required |
|---|---|---|
| `PUBLISH_TOKEN` | pushing to `source_spine` / `game_source` | yes, to publish |
| `APK2SOURCE_S3_ENDPOINT` | artifact bus (e.g. `https://<acct>.r2.cloudflarestorage.com`) | optional |
| `APK2SOURCE_S3_BUCKET` | artifact bus | optional |
| `APK2SOURCE_S3_ACCESS_KEY_ID` | artifact bus | optional |
| `APK2SOURCE_S3_SECRET_ACCESS_KEY` | artifact bus | optional |
| `APK2SOURCE_S3_REGION` | artifact bus (`auto` for R2) | optional |

Vercel backend env: `GITHUB_TOKEN`, `APK2SOURCE_REPO`, `APK2SOURCE_SPINE_REPO`,
`APK2SOURCE_SOURCE_REPO`, `PANEL_ACCESS_KEY`.

Without `PUBLISH_TOKEN` the publish stages automatically fall back to
`--dry-run` and report what they *would* have pushed.

---

## 6. Output contract

`source_spine/<game-slug>/`
```
_index.json                     ← stats + every skeleton entry
spinebot/
  spinebot.json                 ← skeleton (Spine json export)
  spinebot.skel.bytes           ← skeleton (Spine binary export), when present
  spinebot.atlas                ← atlas, page names rewritten to real files
  spinebot.png                  ← texture page(s)
  _meta.json                    ← sha256 of each file, spine version,
                                   bones/slots/animations, source provenance
```

`game_source/<game-slug>/` — AssetRipper `ExportedProject` (Assets/, ProjectSettings/,
C# sources reconstructed from IL2CPP metadata where available).

---

## 7. Legal

The **tooling** in this repository is MIT-licensed and neutral: reverse
engineering for interoperability, research, migration, modding and QA is a
legitimate engineering practice.

The **inputs** are not neutral. Point this pipeline only at builds you own, have
written permission to analyse, or that are openly licensed. Art, audio and code
extracted from a commercial product remain the publisher's property — extracting
them and republishing them into a public repository is copyright infringement,
and running this against a cracked/"patched" build additionally implicates
anti-circumvention law.

See [`docs/LEGAL.md`](docs/LEGAL.md).

---

## 8. Status

| Thing | Where |
|---|---|
| Pipeline repo | https://github.com/leaopold/apk2source |
| Spine output | https://github.com/leaopold/source_spine |
| Unity project output | https://github.com/leaopold/game_source |
| Control panel | repo → Settings → Pages (branch `main`, `/panel` via `pages.yml`) |
| Backend | Vercel (URL recorded in `panel-data/config.json` at build time) |
