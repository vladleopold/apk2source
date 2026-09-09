# Architecture

## 1. Design constraints that shaped everything

| Constraint | Value | Consequence |
|---|---|---|
| GitHub artifact storage (public repo) | 1 GB | A 2–3 GB split-APK set **cannot** move between jobs as an artifact |
| GitHub cache storage (public repo) | 10 GB, 10 GB max per entry | The payload *can* be cached, but only one or two generations |
| `ubuntu-latest` free disk | ~14 GB | Enough for payload + merged tree + extracted assets, not for many copies |
| `ubuntu-latest` job limit | 6 h | A single heavy job must fit; extraction is parallelised |
| Nested virtualisation on Linux hosted runners | not guaranteed | The emulator stage runs on `macos-latest` (hardware acceleration) |
| macOS minutes multiplier | 10× | The emulator stage is opt-in (`run_device_cache`) and `continue-on-error` |

The resulting rule: **big data stays on one runner; only small data crosses job
boundaries.**

## 2. Data flow

```
url ──01 acquire──▶ payload/<file>.apks            (2–3 GB, stays on runner)
                        │  sha256 + .acquire.json
                        ├──▶ actions/cache  (key: payload-<slug>-<sha|urlhash>)
                        └──▶ S3/R2 bus      (key: <slug>/acquire/<file>)   [optional]
                        │
                    02 unpack
                        ├──▶ work/unpacked/splits/*.apk     (base, config.*, ggpack*)
                        ├──▶ work/unpacked/merged/**        (one overlaid tree)
                        └──▶ work/unpacked/meta/*.json      (container + listing census)
                        │
                    02b detect ──▶ work/state/engine_detect.json
                        │
        ┌───────────────┼───────────────────────────────┐
        │               │                               │
   03 device-cache   04 unity-extract              04a/b/c decompilers
   (macOS, parallel) (same runner)                 (parallel, small inputs)
        │               │                               │
   work/device-cache/  work/assets/**              java-source / il2cpp /
     cache/**            │                          ExportedProject
     meta/*.json     05 spine-extract                    │
        │               │                               │
        └────▶ (fed back into 04 as an extra root)      │
                        │                               │
                 work/spine/<game>/**                   │
                        │                               │
                 06 publish ──▶ source_spine    06 publish ──▶ game_source
                        │
                 07 report ──▶ panel-data/runs/<run_id>.json ──▶ Pages
```

`03 device-cache` output is fed back into `04 unity-extract` as a second
`--root`, because a first-run download is very often *more* AssetBundles.

## 3. Why the Spine stage is content-based

Filename-based extraction breaks immediately in the wild:

- AssetBundle container paths are frequently hashed (`a3f9c2.bundle`).
- TextAssets are often named after their GUID, not their content.
- Repackers rename and recompress.
- Some builds ship `.skel.bytes`, others `.json`, some both, some neither
  (atlas-only, with the skeleton inside a `.assets` file).

So every detector sniffs bytes:

| Payload | Detector |
|---|---|
| skeleton json | parses as JSON **and** has `skeleton.spine` **and** `bones` **and** `slots` |
| skeleton binary | Spine `SkeletonInput` varint strings: `[hash][version]` where version matches `^[234]\.\d+(\.\d+)?$`, then two big-endian floats for width/height |
| atlas | full page grammar: non-indented image filename → non-indented `size:`/`format:`/`filter:`/`repeat:` → indented region properties → blank-line page separator |
| texture page | PNG magic |

Pairing precedence is deliberately strict, because a wrong pairing produces a
skeleton that silently renders as a white box:

1. atlas page filename → texture filename (exact, folder-local first, then global)
2. skeleton stem == atlas stem (exact)
3. skeleton stem == texture stem (exact)
4. single-skeleton folder → every image in that folder

Substring/proximity matching was tried and **removed**: it paired `spinebot`
with `spinebot_gear.png`. Exactness plus the single-skeleton-folder fallback
covers the real cases without that class of error.

## 4. Why container paths are preserved

`AssetBundle.m_Container` maps logical paths (`assets/spine/hero.json`) to
`path_id`s. `unity_extract` inverts that map and writes each object under its
container path instead of a flat `TextAsset/` bucket. That single decision is
what puts `hero.json`, `hero.atlas` and `hero.png` in the **same directory**,
which is what makes folder-local pairing (rule 1 and 4 above) work.

Without it, a bundle with 40 skeletons and 40 atlases becomes an unpairable pile.

## 5. Texture orientation

Unity stores `Texture2D` pixel data bottom-up. `UnityPy` flips it on export, so
an exported PNG is visually correct but its raw RGBA digest differs from the
bytes stored in the container. The fixture manifest records **both** digests
(`rgba_sha256` and `rgba_sha256_flipped`) and the selftest asserts against the
flipped one. This is a real trap: verifying against the unflipped digest makes a
correct pipeline look broken.

## 6. Binary TextAssets

Spine `.skel.bytes` payloads contain arbitrary bytes. Unity serialises
`TextAsset.m_Script` as a length-prefixed **string**, and UnityPy hands it back
as `str`. Round-tripping arbitrary bytes through `str` requires
`encode("utf-8", "surrogateescape")` on the way out — a plain `.encode()` raises
`UnicodeEncodeError` on the first lone surrogate. `unity_extract` does this
explicitly.

## 7. The artifact bus

`bus.py` implements AWS SigV4 directly (≈70 lines) rather than pulling boto3:

- keeps the CI image small and the dependency surface auditable;
- Cloudflare R2 and MinIO are S3-compatible, so one implementation covers all three;
- single `PUT` up to 5 GiB covers every realistic APK; ranged `GET` gives resume.

`plan` probes the four `APK2SOURCE_S3_*` secrets and exports `bus_enabled`.
When false, the orchestrator uses `actions/cache` + re-download instead. The
pipeline is correct either way; the bus is a speed optimisation, not a
dependency.

## 8. Panel ↔ backend trust model

```
browser ──(no token)──▶ Pages static SPA
   │                         │
   │  X-Panel-Key            │  reads ./data/*.json (baked, public)
   ▼                         ▼
Vercel backend ──(GITHUB_TOKEN in env)──▶ GitHub API
```

- The GitHub token exists **only** in Vercel env. It is never inlined into the
  page, never in a client bundle, never in `panel-data/`.
- Read endpoints are open (the underlying repos are public anyway).
- Write endpoints (`/api/trigger`, `/api/stage`) require `X-Panel-Key` when
  `PANEL_ACCESS_KEY` is set, and whitelist both the workflow filename and the
  input keys. An attacker who finds the panel cannot dispatch an arbitrary
  workflow with arbitrary inputs.
- `/api/tree` allow-lists repository owners, so the proxy cannot be used to
  enumerate arbitrary GitHub repos.
- The SPA degrades to read-only static mode when the backend is unreachable.

## 9. Failure modes and how they are handled

| Failure | Handling |
|---|---|
| Download dies at 2.1 GB of 2.8 GB | `Range` resume from the `.part` file, 5 retries with exponential backoff |
| Mirror 404s | `--url` is repeatable; sources are tried in order |
| Payload corrupted in transit | optional `--sha256` fails the stage loudly |
| `.apks` has no `base.apk` | first `.apk` member is promoted to base, warning emitted |
| Split ABI does not match the emulator | ABI splits are filtered out before `install-multiple` |
| App never downloads anything on first run | `cache = post_run − post_install` is simply empty; stage succeeds with 0 files |
| Emulator never boots | `find_device` times out, stage returns `degraded: true`, pipeline continues |
| Encrypted / unsupported AssetBundle | per-file `errors[]` in `_unity_report.json`, other files still extracted |
| One object type blows up mid-file | per-object try/except, capped at 200 recorded errors, rest of the file continues |
| Skeleton with no atlas | exported as `incomplete`, counted separately, never silently dropped |
| Atlas with no skeleton | exported as an `orphan_atlas` entry — still useful |
| Same skeleton in 3 bundles | content-hash dedupe, `duplicate_of` provenance retained |
| Destination repo has 40 GB of history | `--filter=blob:none` + sparse-checkout of just the target subdir |
| Push rejected (race) | 4 retries with backoff |
| No `PUBLISH_TOKEN` | publish stages auto-fall back to `--dry-run` and report the diff |

## 10. Determinism

The fixture generator is fully deterministic (CRC32-mixed pixel field, SHA1-derived
path ids), so `selftest` produces byte-identical output on every run and every
runner. That is what makes "12/12 byte-exact" a meaningful regression gate
rather than a lucky pass.
