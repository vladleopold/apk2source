# Runbook

## First-time setup

### 1. Secrets (repo → Settings → Secrets and variables → Actions)

```bash
export GH_TOKEN=<your PAT with repo+workflow scope>
R=leaopold/apk2source

# required to publish results
gh secret set PUBLISH_TOKEN       --repo "$R" --body "<PAT with repo scope>"

# optional: artifact bus for multi-GB transfer between workflows
gh secret set APK2SOURCE_S3_ENDPOINT          --repo "$R" --body "https://<account>.r2.cloudflarestorage.com"
gh secret set APK2SOURCE_S3_BUCKET            --repo "$R" --body "apk2source"
gh secret set APK2SOURCE_S3_ACCESS_KEY_ID     --repo "$R" --body "<key id>"
gh secret set APK2SOURCE_S3_SECRET_ACCESS_KEY --repo "$R" --body "<secret>"
gh secret set APK2SOURCE_S3_REGION            --repo "$R" --body "auto"
```

### 2. Variables (repo → Settings → Secrets and variables → Actions → Variables)

```bash
gh variable set APK2SOURCE_BACKEND_URL --repo "$R" --body "https://<your-vercel-app>.vercel.app"
gh variable set APK2SOURCE_SPINE_REPO  --repo "$R" --body "leaopold/source_spine"
gh variable set APK2SOURCE_SOURCE_REPO --repo "$R" --body "leaopold/game_source"
```

### 3. GitHub Pages

`pages.yml` deploys with `actions/deploy-pages`, so Pages must be set to
**GitHub Actions** as the source:

```bash
gh api -X POST "/repos/$R/pages" -f "build_type=workflow" 2>/dev/null || \
gh api -X PUT  "/repos/$R/pages" -f "build_type=workflow"
```

### 4. Vercel backend

```bash
cd backend
npx vercel link
npx vercel env add GITHUB_TOKEN            production   # PAT with actions:write + repo read
npx vercel env add PANEL_ACCESS_KEY        production   # random string, also entered in the panel ⚙
npx vercel env add APK2SOURCE_REPO         production   # leaopold/apk2source
npx vercel env add APK2SOURCE_SPINE_REPO   production   # leaopold/source_spine
npx vercel env add APK2SOURCE_SOURCE_REPO  production   # leaopold/game_source
npx vercel deploy --prod
```

Then open the Pages URL → **⚙ Settings** → paste the backend URL and the panel
access key. Both stay in `localStorage` on that browser only.

Generate a key:
```bash
openssl rand -hex 24
```

---

## Operating

### Dispatch a full run

Panel → **Run** tab → fill the form → **Dispatch pipeline**.

Or CLI:
```bash
gh workflow run pipeline.yml \
  -f url='https://…/game.apks' \
  -f game_name='my-game' \
  -f use_cache=true \
  -f run_java_decompile=true \
  -f run_il2cpp=true \
  -f run_assetripper=true \
  -f run_device_cache=false \
  -f publish_spine=true \
  -f publish_game_source=false
```

Or from any external system:
```bash
curl -X POST "https://api.github.com/repos/leaopold/apk2source/dispatches" \
  -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
  -d '{"event_type":"apk2source-run","client_payload":{"url":"https://…","game_name":"my-game"}}'
```

### Re-run a single level

```bash
gh workflow run stage.yml -f stage=spine -f game_name=my-game
```
Stages: `acquire unpack detect unity spine publish-spine publish-source selftest`.

### Watch a run

```bash
gh run watch
gh run list --workflow pipeline.yml
gh run view <id> --log-failed
```

### Fetch results without the panel

```bash
gh run download <id> -n spine-output -D ./out
```

---

## Cache management

| Cache | Key | Cleared by |
|---|---|---|
| `actions/cache` payload | `payload-<slug>-<sha256 or url-hash>` | repo → Settings → Actions → Caches → delete |
| On-disk cache dir | `<cache-dir>/<2-char>/<name>` + `.meta.json` | `--no-cache` on the run, or delete the dir |
| AVD snapshot | `avd-<api>-<arch>` | repo → Settings → Actions → Caches |
| Tool downloads | `$APK2SOURCE_TOOLS` | delete the directory |

Run with **Use payload cache** off to force a fresh download; the new payload
overwrites the cache entry at the end of the acquire stage.

---

## Troubleshooting

**`no Spine skeletons found`**
The build may genuinely have no Spine content. Check
`work/state/engine_detect.json → spine_signals` and `work/assets/_unity_report.json → totals`.
If `TextAsset` is 0, the Unity containers were not readable (encrypted bundle,
unsupported Unity version, or the assets live in an OBB/asset pack that was not
merged).

**Unity extraction returns 0 files**
`detect` will show `engine != unity`. For Unreal look at `*.pak`; for libGDX the
Spine assets are loose files under `assets/` and stage 05 can run directly on
the merged tree:
```bash
apk2source extract-spine --assets work/unpacked/merged --out work/spine --game my-game
```

**`install-multiple` fails with INSTALL_FAILED_...**
Usually an ABI mismatch or a missing split. The stage already filters ABI
splits; if it still fails, inspect `work/device-cache/meta/device_cache.json →
install.stderr` and try `--skip-install` against a manually installed build.

**Emulator never boots on the runner**
Expected on Linux hosted runners without KVM. The stage runs on `macos-latest`;
if it still fails it is `continue-on-error` and the rest of the pipeline is
unaffected.

**Publish says `dry_run`**
`PUBLISH_TOKEN` is not set. The stage still reports exactly which files it would
have pushed.

**Push rejected / remote too large**
Publishing uses `--filter=blob:none` + sparse-checkout. If the destination repo
has LFS objects, install `git-lfs` on the runner (the publish stage enables it
automatically when the largest file ≥ 8 MiB).

**Panel shows "backend: offline"**
Open ⚙ Settings and set the backend URL. The panel still works read-only from
the baked `panel-data/index.json`, which `pages.yml` refreshes every 30 minutes.

**`Dispatch failed: HTTP 401`**
`PANEL_ACCESS_KEY` is set on Vercel but the panel key is missing/wrong. Re-enter
it in ⚙ Settings.

---

## Cost notes

| Job | Runner | Typical cost driver |
|---|---|---|
| heavy | ubuntu-latest | disk + wall time on a 2–3 GB payload |
| device-cache | macos-latest | **10× minute multiplier** — off by default |
| java/il2cpp/unity-project | ubuntu-latest | CPU-bound, parallel |
| report | ubuntu-latest | seconds |

Leave `run_device_cache` off unless you specifically need the first-run
download. The static path already reaches everything shipped inside the
container.
