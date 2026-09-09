# Pipeline reference

Every stage is one CLI subcommand. A GitHub Actions step and a local debug
session run the identical code path.

```bash
python -m pip install -e ./tools
apk2source <stage> --help
```

---

## 01 · acquire

```bash
apk2source acquire --url URL [--url URL2] --out DIR [--cache-dir DIR]
                   [--no-cache] [--sha256 HEX] [--filename NAME] [--work DIR]
```

| Behaviour | Detail |
|---|---|
| Resume | `Range: bytes=N-` from an existing `.part` file |
| Retries | 5, exponential backoff capped at 60 s |
| Mirrors | `--url` is repeatable; tried in order |
| Verify | `--sha256` fails the stage on mismatch |
| Cache | `<cache-dir>/<2-char>/<name>` + `<name>.meta.json` record |
| Cache hit rule | same URL **and** (no expected sha256 **or** matching sha256) **and** recorded size == actual size |
| `file://` | supported, so the selftest uses the real code path |

Outputs: `payload_path`, `payload_sha256`, `payload_size`, `payload_cached`.

---

## 02 · unpack

```bash
apk2source unpack --input FILE --out DIR [--drop-splits] [--work DIR]
```

Detection is content-based:

| Container | Signature |
|---|---|
| `.apks` | zip containing `base.apk` and/or `split_*` members |
| `.xapk` | zip containing `manifest.json` + `*.apk` |
| `.apkm` | zip containing `info.json` + `*.apk` |
| `.aab` | zip containing `BundleConfig.pb` or `base/manifest/AndroidManifest.xml` |
| `.apk` | zip containing `AndroidManifest.xml` |

Split roles:

| Role | Match |
|---|---|
| `base` | `base.apk`, `base-master.apk` |
| `config` | `split_config.*`, dpi/locale/ABI patterns |
| `asset_pack` | everything else (`split_ggpack1.apk`, …) |

Merge order: base → configs → asset packs (later wins). Per-split
`META-INF/*.{SF,RSA,DSA,EC,MF}` are skipped so signatures do not collide.

Outputs: `merged/`, `splits/`, `meta/unpack.json`, `meta/merged_listing.json`.

The package id is recovered from the binary `AndroidManifest.xml` string pool by
a built-in AXML parser — no `aapt2` required.

---

## 02b · detect

```bash
apk2source detect --merged DIR [--out FILE] [--work DIR]
```

Evidence table (excerpt):

| Engine | Evidence |
|---|---|
| Unity | `assets/bin/Data/data.unity3d`, `globalgamemanagers`, `*.assets`, `*.resS`, `assets/aa/**`, `libil2cpp.so`, `libunity.so`, `*.bundle`, `UnityFS\0` magic |
| Unreal | `assets/UnrealGame/**`, `**/Paks/**`, `*.pak`, `libUnreal.so`, `libUE4.so` |
| Godot | `*.pck`, `libgodot_android.so` |
| libGDX | `libgdx*.so`, loose `assets/**/*.atlas` |
| Cocos | `assets/src/**/*.jsc`, `libcocos2djs.so` |
| Defold | `*.dmanifest`, `*.luac` |
| Corona/Solar2D | `assets/main.lua`, `libcorona.so` |

Also reports: `abis`, `native_libs`, `dex_count`, `has_resources_arsc`,
`unity_version`, `spine_signals`.

`unity_version` is probed from `globalgamemanagers`, `data.unity3d`, raw
SerializedFile headers and UnityFS headers, in that order.

---

## 03 · device-cache

```bash
apk2source device-cache --splits DIR --out DIR [--package PKG] [--serial S]
                        [--settle SECONDS] [--no-pull] [--skip-install] [--work DIR]
```

Sequence:

1. `find_device` (waits up to 300 s)
2. `device_info` — model, Android version, SDK, ABI, RAM
3. `install` — `install-multiple` for split sets, ABI splits filtered to the device ABI
4. `resolve_package` — explicit `--package`, else the newest third-party package
5. `snapshot pre_install`
6. `launch` — `cmd package resolve-activity --brief` → `am start -n`, `monkey` fallback
7. `wait_for_idle` — poll the data dirs until the total size is stable for 3 consecutive polls (default interval 15 s, cap 900 s)
8. `snapshot post_run`
9. `cache = post_run − post_install`, `shipped = post_install − pre_install`
10. `adb pull` every cache path into `out/cache/`

Scanned roots (no root access needed):
```
/sdcard/Android/data/<pkg>     /sdcard/Android/obb/<pkg>
/sdcard/<pkg>                  /data/data/<pkg>     /data/user/0/<pkg>
```

Emitted: `meta/device.json`, three `snapshot_*.json`, `cache_manifest.json`,
`shipped_manifest.json`, `device_cache.json`.

**No device?** The stage returns `degraded: true` and the pipeline continues —
the static path does not depend on it.

---

## 04 · unity-extract

```bash
apk2source extract-unity --root DIR [--root DIR2] --out DIR
                         [--jobs N] [--types TextAsset --types Texture2D]
                         [--flat] [--limit N] [--max-texture-side PX] [--work DIR]
```

Discovery: known Unity paths → Unity-ish suffixes → **magic-byte sniffing** for
renamed or extensionless blobs.

Exported types: `TextAsset`, `Texture2D`, `Sprite`, `MonoBehaviour`, `Mesh`,
`AudioClip`, `VideoClip`, plus counters for `Material`/`Shader`/`Font`/`AnimationClip`.

Layout rules:
- If the object's `path_id` is in `AssetBundle.m_Container`, write under the
  container path (last 4 segments). **This is what keeps Spine triplets together.**
- Otherwise write under a per-type folder.
- Collisions get a `__N` suffix rather than overwriting.

TextAsset extension inference: a name already ending in a known suffix is kept
verbatim; otherwise `{` → `.json`, `<` → `.xml`, NUL in the first 4 KiB →
`.bytes`, else `.txt`.

Parallelism: `ProcessPoolExecutor` across files, `--jobs` defaults to
`min(8, cpu_count)`.

Report: `_unity_report.json` with per-file counts, timings and up to 50 errors
each.

---

## 04a/b/c · decompilers

```bash
apk2source survey     --merged DIR [--out FILE]
apk2source decompile  --merged DIR --assets DIR --out DIR [--apk FILE]
                      [--tools apktool --tools jadx --tools assetripper --tools il2cppdumper]
```

Tools self-provision from their official GitHub releases into
`$APK2SOURCE_TOOLS` (default `~/.cache/apk2source/tools`) on first use, with
platform mapping for macOS / Linux / Windows.

| Tool | Input | Output |
|---|---|---|
| apktool | `.apk` | decoded resources, smali, readable `AndroidManifest.xml` |
| jadx | `.apk` | Java sources (`--no-res --show-bad-code`) |
| AssetRipper | merged tree / assets | `ExportedProject` — an openable Unity project |
| Il2CppDumper | `libil2cpp.so` + `global-metadata.dat` | `DummyDll`, `Dump.cs`, `il2cpp.h` |

`survey` tells you up front which of these are actually applicable
(`can_jadx`, `can_il2cpp`, `can_assetripper`).

Every wrapper is best-effort: a tool that cannot run is recorded, never fatal.

---

## 05 · spine-extract

```bash
apk2source extract-spine --assets DIR --out DIR --game NAME [--package PKG] [--work DIR]
```

Scan → classify → dedupe → group → export.

| Step | Detail |
|---|---|
| Scan | skips known-binary extensions, caps at 200 MB/file |
| Classify | json / skel / atlas / image, by content |
| Dedupe | sha256 per (kind, content); duplicates recorded as `duplicate_of` |
| Group | folder-local, then the 4-level pairing precedence |
| Merge | `<name>` json + `<name>_SkeletonData` skel → one entry |
| Disambiguate | duplicate stems get `~<origin-bundle>` |
| Export | `<out>/<game-slug>/<skeleton>/…` + `_meta.json` + `_index.json` |

Atlas page filenames are rewritten to the names actually written to disk, so the
exported atlas resolves.

Exit code is `2` when zero skeletons are found (the workflow turns that into a
warning, not a failure — plenty of games simply have no Spine content).

---

## 06 · publish

```bash
apk2source publish --src DIR --repo owner/name [--subdir PATH] [--branch main]
                   [--message MSG] [--mode direct|pr] [--no-lfs] [--only .json]
                   [--dry-run] [--workdir DIR] [--work DIR]
```

| Behaviour | Detail |
|---|---|
| Clone | `--filter=blob:none --no-checkout --depth 1` + `sparse-checkout set <subdir>` |
| Fallback | if the sparse clone fails (empty repo), a fresh repo is initialised |
| LFS | auto-enabled when the largest file ≥ 8 MiB; tracks 20 binary patterns |
| Identity | `apk2source-bot <apk2source-bot@users.noreply.github.com>` |
| Auth | `https://x-access-token:<token>@github.com/…`, token from env, never logged |
| Idempotence | no changes → no commit, `reason: "no changes"` |
| Retries | 4 pushes with backoff |
| PR mode | pushes to `apk2source/<slug>-<ts>` and opens a PR |
| No token | callers pass `--dry-run`; the record includes the would-be file list |

---

## 07 · report

```bash
apk2source report --work DIR --game NAME [--run-id ID] [--out FILE]
```

Folds every `work/state/<stage>.json` into `work/pipeline.json` (schema 1) and
writes a Markdown table to `$GITHUB_STEP_SUMMARY`. `pages.yml` copies these into
`panel-data/runs/<run_id>.json` and merges them into `panel-data/index.json`.

---

## Artifact bus

```bash
apk2source bus-put --src FILE --key game/stage/name
apk2source bus-get --key game/stage/name --dest FILE
apk2source bus-ls  [--prefix game/]
apk2source bus-rm  --key game/stage/name
```

Backend selection: `$APK2SOURCE_BUS` = `none` (default) | `local` | `s3`.
S3 needs `APK2SOURCE_S3_{ENDPOINT,BUCKET,ACCESS_KEY_ID,SECRET_ACCESS_KEY,REGION}`.

---

## Self-test

```bash
apk2source selftest --work DIR [--game NAME] [--unity 2021.3.16f1]
```

Builds the fixture, wraps it in a synthetic `.apks`, and drives
acquire → unpack → detect → unity → spine → report, asserting 21 checks
including byte-exact sha256 comparison of every exported json / skel / atlas /
png against the generator's manifest.
