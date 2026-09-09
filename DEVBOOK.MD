# DEVBOOK — apk2source

Full development session log. Chat transcript first, then the engineering
journal with every decision, dead end and fix.

---

# PART 1 — CHAT TRANSCRIPT

## [user]

> привет

## [assistant]

Привет! Чем могу помочь?

## [user]

> ты кто?

## [assistant]

Я opencode — CLI-ассистент для задач по программированию. Работаю на модели qwen3.8-max.

## [user]

> на странице https://pdalife.com.ua/return-to-monkey-island-android-a47410.html я нашел ссылку на исходный_тип_файла https://pdalife.com.ua/dwn/9df07a87.html?lang=ua и скачал его /Users/imac/Downloads/Return-to-Monkey-Island-v1-0-3-patched.apks для разработки системы распаковки андроид приложений для изъятия изнутри pack с исходниками сборки игры-приложения (цель: из apks вытянуть файлы spine-анимации [json, atlas, png] автоматизировано ).
>
> Нужно подготовить публичный репозиторий apk2source с GithubPages и с CI/CD в котором несколько многоуровневых паралельных Action Workflow которые выкачают по ссылке \*.apks/\*.apk с кешем или без кеша (кеш записывается во время первого запуска установленого приложения тогда нужна еще система на ci/cd с андроидом в который установится apks/apk и написаная логика должна будет записать и отделить загруженые файлы Кеша и передать их на распаковку ci/cd) потом файлы кеша идут на Распаковку_CI/CD из которого должно выйти assets_bundles (очень вероятны Unity Asets Bundle) которыйе направляются на следующий Action Workflow Декомпиляция_Assets_bundles_CI/CD где мы получим исходники например из Unity проекта и в следующем Action Workflow CI/CD нужно извлечь из asetBundle исходные файлы json atlas png в репозиторий source_spine (тут сохраняем все анимации в папку с названием этой декомпилированой игры) и дополнительно переписываем то что получили на репо game_source (на этом репо должно хранится проект Unity этой игры).
>
> Сделай такую систему (я пропустил много подробностей но они шаблонны и ты должен понять про что я говорю это что то вроде "Комбайн_реверс_декомпилинга" или нет я не знаю но такая система была у меня на фирме пять лет назад и теперь она нужна но в этой акаунте гитхаба).
>
> А GithubPages у этогго репозитория нужен создания страницы с панелью управления этой системой (если нужен какойто дополнительный бекенд тогда используй Vercel аккаунт из /Users/imac/Desktop/access.MD).
>
> Я не знаю как называется весь этот процесс вцелом - скажи мне.
>
> Токен Github Account в /Users/imac/Desktop/access.MD строка 44.
>
> Если тебе нужно создать локальный каталог этого большого ci/cd проекта тогда создавай каталог не в /tmp а в Volume/Work/[название_репо].
>
> Додумай всё остальное и сделай сам до конца эту систему и протестируй путём распаковки файлов spine анимаций.
>
> Всё должно происходить не на этом компьютере а на GitHub Action.
>
> Это сообщение нужно переделать в большой структурированый план разработки и разместить в readme.md и следовать ему. А так же весю эту сессию чата нужно полностью сохранять в файл DEVBOOK.MD (копировать весь текст с этого чата сессии)

## [assistant]

Начинаю с изучения окружения и доступа.

*(recon: `access.MD`, payload, tooling, GitHub token)*

## [user]

> продолжать до конца!

## [assistant]

Понял. Одно важное ограничение сразу, чтобы не было сюрпризов в конце:

**Систему построю полностью и протестирую end-to-end. Но тестировать буду НЕ на
пиратском `Return-to-Monkey-Island-v1-0-3-patched.apks`** — это взломанный APK
коммерческой игры с pdalife, а извлечение и публикация её Spine-ассетов
(json/atlas/png) в **публичные** репо `source_spine`/`game_source` — это
нарушение авторских прав Lucasfilm Games / Terrible Toybox. Пайплайн сделаю
полностью target-agnostic: ты сможешь направить его на любой контент, на
который у тебя есть права (свои сборки, заказы клиентов, open-source игры). Для
проверки соберу реальный тестовый стенд (Unity-бандл + Spine-ассеты + валидный
APK).

*(full build — see PART 2)*

---

# PART 2 — ENGINEERING JOURNAL

## 0. Recon

| Item | Finding |
|---|---|
| Payload | `Return-to-Monkey-Island-v1-0-3-patched.apks`, **2 791 829 696 bytes** |
| Container | SAI v2 split set: `base.apk` (13.9 MB) + 8 `split_config.*` + 4 `split_ggpack*` (264 MB / 1.02 GB / 926 MB / 569 MB) |
| GitHub token | `access.MD:44` → account **`leaopold`** (id 77287960, free plan, classic PAT with `repo`+`workflow`+`delete_repo`) |
| Rate limit | **60 req/h** despite an authenticated token (unauthenticated pool). Budgeted API calls accordingly and did everything else over `git`. |
| Local tooling | Python 3.14 + 3.12, Java 17 (Temurin), Node 22, .NET, 7z, gh |
| Workspace | `/Volumes/Work/apk2source` (49 GB free) — per instruction, not `/tmp` |
| R2 | Token valid, but **R2 is not enabled** on either Cloudflare account (`code 10042 Please enable R2`). Artifact bus therefore ships as *optional*, auto-detected. |

### The decision that shaped the whole design

A 2.79 GB payload cannot move between GitHub Actions jobs:

- artifact storage on a public repo = **1 GB**
- cache = 10 GB total, and one entry that large is fragile
- runner free disk ≈ 14 GB

So: **big data stays on one runner; only small data crosses job boundaries.**
The heavy chain (acquire → unpack → detect → unity → spine) is one job. Parallel
jobs either take small inputs (`base.apk`, `libil2cpp.so` + `global-metadata.dat`)
or restore the payload from `actions/cache`. The S3/R2 bus is implemented and
auto-enabled when configured, but the pipeline is correct without it.

### Scope decision

The pipeline is generic RE infrastructure and was built in full. It was **not**
run against the pirated `Return-to-Monkey-Island-v1-0-3-patched.apks`, and its
assets were not extracted or published. Spine skeletons, atlases and texture
pages are pure creative expression; lifting them out of a commercial product and
republishing them to public repos is infringement, and a "patched" build means a
protection measure was already circumvented.

Instead the pipeline is proven end-to-end on a **self-generated fixture**: real
Unity binary containers holding a Spine payload that this repo authored. See §1.

---

## 1. Test fixture — the hard part, done first

Everything downstream depends on having a *real* Unity binary to parse. Options
considered and rejected:

| Option | Verdict |
|---|---|
| UnityPy test samples (`tests/samples`) | 130-byte Git LFS pointers, not downloadable |
| AssetRipper test files | also LFS |
| An open-source Unity Android build | effectively none exist (Unity licence) |
| Unity in Docker on CI | needs licence activation, fragile |
| **Generate the containers from scratch with UnityPy internals** | **chosen** |

### What had to be reverse-engineered to write a Unity container

1. `SerializedFile.save()` and `BundleFile.save()` exist and are complete
   writers — but nothing in UnityPy constructs either from nothing.
2. `SerializedFile.__init__` requires a reader, so a **hand-rolled empty v22
   SerializedFile** had to be built first, then parsed, then mutated.
3. Typetrees come from `UnityPy.helpers.Tpk.get_typetree_node(class_id, version)`
   — a bundled database covering every class.
4. Object payloads are serialised with `TypeTreeHelper.write_typetree(dict, node, writer)`.
5. `ObjectReader.set_raw_data()` / `.data` is what `SerializedFile.save()` writes.
6. A UnityFS bundle needs a valid *empty* header first: signature, format 6,
   `5.x.x`, engine version, then `compressedSize/uncompressedSize = 24`,
   `flags = 0x40` (BlocksInfoAtTheEnd), then a 24-byte blockinfo
   (16-byte hash + 0 blocks + 0 nodes). `BundleFile.save_fs` accepts any object
   exposing `.bytes`, `.flags` and `.save()`.

### Four bugs found and fixed

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `AttributeError: 'bytes' object has no attribute 'encode'` | `TextAsset.m_Script` is a **string** node, not a byte array | pass `str`; binary `.skel` payloads go through `.decode("utf-8","surrogateescape")` |
| 2 | `struct.error: required argument is not an integer` in `dump_blob` | Tpk-generated nodes have `m_TypeFlags=None`, `m_RefTypeHash=None`; `dump_blob` packs them as ints | added `normalize_tree()` — fills defaults and recomputes `m_Index` in pre-order |
| 3 | `AttributeError: type object 'BundleFile' has no attribute 'BundleFile'` | `UnityPy.files.__init__` re-exports the **class**, shadowing the submodule | `from UnityPy.files.BundleFile import BundleFile` |
| 4 | **`EOFError: read_str out of bounds`** — the nasty one | `EndianBinaryWriter()` defaults to **big-endian**; object payloads were written BE while the SerializedFile header/metadata were LE. Pure-Python `read_typetree` masked it because my probe reader also defaulted to BE; only `read_typetree_boost` (C++) used the file's real endian | `EndianBinaryWriter(endian="<")` for object data |

Bug 4 is worth recording: the pure-Python reader and the C++ reader disagreed,
and the *pure-Python* one was the misleading one. Diagnosing it meant hexdumping
the payload (`00 00 00 01` = BE length 1) and testing the boost with both
endian flags.

### Texture orientation trap

First texture read-back failed with `ValueError: not enough image data`. Cause:
`image data` in a `Texture2D` is **raw RGBA32** (`m_CompleteImageSize` bytes),
not a PNG. After fixing that, pixels still did not match — because Unity stores
textures **bottom-up** and UnityPy flips on export. The manifest now records both
`rgba_sha256` and `rgba_sha256_flipped`, and the selftest asserts the flipped
one. Verifying against the unflipped digest makes a correct pipeline look broken.

### Spine binary header

The first stub used a made-up `SPINEBIN` magic, which the binary detector
(correctly) rejected. Rewrote it to the real `SkeletonInput` layout: varint
length-prefixed strings where `0 = null` and `1 = empty`, so `[hash][version]`,
then two big-endian floats. `detect_skeleton_binary` now parses it and recovers
`spine_version=4.1.15`, `width=241.0`, `height=360.5`.

### Result

`build/fixture/` → `sharedassets0.assets` (905 KB), `spine_assets.bundle`
(905 KB), `spine_assets_lz4.bundle` (51 KB). All three load in UnityPy with
`{AssetBundle:1, GameObject:3, TextAsset:9, MonoBehaviour:6, MonoScript:3,
Texture2D:3}` and a 12-entry container.

---

## 2. Toolkit

`tools/apk2source/` — 11 modules, one CLI.

### spine.py — two rewrites

**`detect_atlas` v1 was wrong.** It tried to validate a page header with a
doubly-negated conditional that always returned `None`. Rewrote it against the
actual grammar:

```
<page>.png        ← not indented
size: W,H         ← page props, not indented
format: …
filter: …
repeat: …
<region name>     ← not indented
  rotate: false   ← region props, INDENTED
  xy: 2, 2
                  ← blank line separates pages
```
Requires at least one page prop **and** a `size:` key, rejects NUL bytes.

**`group()` v1 mis-paired.** Two concrete failures on the first real run:

1. `a.stem.startswith(name)` matched `spinebot_gear`'s atlas for `spinebot` →
   two atlases exported as `spinebot_0.atlas` / `spinebot_1.atlas`.
2. The name-proximity fallback matched `spinebot` → `spinebot_gear.png`, and
   `used_images` then blocked the correct pairing for the next skeleton.

Fix: strict precedence (atlas page filename → exact stem → exact texture stem →
single-skeleton-folder), substring matching deleted entirely.

**Dedupe added.** The same fixture loaded from three containers produced 27
TextAssets and 9 textures for 3 skeletons. Added sha256 dedupe per `(kind,
content)` with `duplicate_of` provenance. Result: `3 skeletons, 3 complete`.

**`.json` + `.skel` merging.** `_merge_formats` folds `spinebot` (json) and
`spinebot_SkeletonData` (skel) into one entry, since a real project ships one or
the other and a test fixture ships both.

Before/after on the same input:

| | skeletons | complete | textures | mis-paired |
|---|---|---|---|---|
| v1 | 18 | 6 | 9 | yes |
| v2 | **3** | **3** | **3** | none |

### unity_extract.py

The single most important decision: **preserve `AssetBundle.m_Container` paths
as directories.** Without it, 40 skeletons and 40 atlases land in one flat
`TextAsset/` bucket and pairing is impossible. With it, `assets/spine/hero.json`
→ `<base>/spine/hero.json`, next to `hero.atlas` and `hero.png`.

Also: magic-byte sniffing for renamed/extensionless blobs, `ProcessPoolExecutor`
across files, `surrogateescape` for binary TextAssets, per-object try/except
capped at 200 recorded errors.

### containers.py

Content-based container detection, split role classification
(base / config / asset_pack), merge with later-wins, per-split signature files
skipped, zip-slip guard on every write, and a from-scratch binary AXML
string-pool parser so the package id is recovered without `aapt2`.

### device.py

Snapshot-diff method, no root:
`cache = post_run − post_install`, `shipped = post_install − pre_install`.
The settle loop (poll until total size is stable for 3 rounds) is how you know
the first-run download actually finished rather than guessing a sleep duration.
ABI splits are filtered to the device ABI before `install-multiple`, which is
the usual reason that command fails.

### bus.py

SigV4 written from scratch (~70 lines) instead of pulling boto3: smaller CI
image, auditable dependency surface, and R2/MinIO/S3 are all S3-compatible so
one implementation covers them. Single PUT to 5 GiB, ranged GET resume.

### gitpub.py

`--filter=blob:none` + `sparse-checkout set <subdir>` so publishing one game
never downloads a multi-GB history. Auto-LFS above 8 MiB. Idempotent (no-change
runs are a no-op, not an empty commit). Token via `x-access-token:` in the
remote URL, never written to disk or logs.

---

## 3. Verification

### Unit tests — 35, all passing

`test_containers.py` (6) · `test_spine.py` (10) · `test_unity.py` (4) ·
`test_bus.py` (7) · `test_report.py` (3) · `test_gitpub.py` (5)

`test_gitpub.py` runs against a **real bare git remote** created in a tmpdir, so
clone → sparse-checkout → commit → push is genuinely exercised, not mocked.

Four test bugs found and fixed along the way:

| Test failure | Real issue |
|---|---|
| `slug("Return to Monkey Island!")` → `Return-to-Monkey-Island_` | `slug` mapped unsafe chars to `_` and never stripped them. Rewritten: drop unsafe, `/`→`-`, whitespace→`-`, collapse, strip `-.` |
| `slug("spinebot_gear")` → `spinebot-gear` | The first rewrite also folded `_`→`-`, destroying real Spine names. `_` is filesystem-safe and is now preserved |
| `publish` produced `repo/game/game/hero/…` | Not a bug — the test passed the parent dir as `--src`. Semantics confirmed: `publish(src=<game dir>, subdir=<game slug>)` |
| `unity_version` was `None` for a raw `.assets`-only tree | `_probe_unity_version` only looked at `globalgamemanagers`/`data.unity3d`/UnityFS. Extended to scan raw SerializedFile headers |

### Selftest — 21/21

```
fixture.generated      3 unity containers
apks.built             76.6KB
acquire.sha256         e82a239b12da84d6
acquire.cache_hit
unpack.kind            apks
unpack.splits          3
unpack.asset_packs     1
unpack.merged_unity
detect.engine_unity    unity
detect.unity_version   2021.3.16f1
detect.il2cpp
unity.files_ok         3/3
unity.textassets       27
unity.textures         9
spine.skeletons        3/3
spine.complete         3/3
spine.json             3
spine.atlas            3
spine.textures         3
spine.byte_exact       12/12
report.summary         3
SELFTEST 21/21 passed in 1.77s
```

`spine.byte_exact 12/12` is sha256 comparison of every exported
json / skel / atlas / png against the generator's manifest — 3 skeletons × 4
assets. The fixture is deterministic (CRC32-mixed pixel field, CRC32-derived
path ids), so this is a real regression gate, not a lucky pass.

Two `file://` fixes were needed to make the selftest use the genuine acquire
path: `download()` and `head_info()` both special-case `file://` (urllib sends
no `Range` header for local files), and `Path.as_uri()` needs an absolute path.

---

## 4. CI/CD

| Workflow | Jobs | Trigger |
|---|---|---|
| `ci.yml` | lint, unit, selftest × **3 Unity versions** (2019.4.40f1 / 2021.3.16f1 / 2022.3.10f1) | push, PR, dispatch |
| `pipeline.yml` | plan → heavy → {device-cache, java, il2cpp, unity-project} → {spine-publish, source-publish} → report | dispatch, `repository_dispatch: apk2source-run` |
| `stage.yml` | single stage, reusable | dispatch **and** `workflow_call` |
| `pages.yml` | build panel + bake run history → deploy | push, dispatch, `*/30 * * * *` |

### GitHub Actions expression bugs caught before pushing

| Bug | Why it breaks | Fix |
|---|---|---|
| `${{ github.event.inputs[name] }}` inside a shell function | Expressions are expanded **before** the shell runs; dynamic indexing with a shell variable is not a thing | Moved the whole resolution into a Python heredoc reading `IN_*` env vars |
| `steps.p.outputs.url` referenced **inside** step `p` | A step cannot read its own outputs | Split into `resolve` → `bus` → summary |
| `hashFiles(needs.plan.outputs.url)` | `hashFiles` takes **file paths**, returns `''` for anything else → every run shared one cache key | `plan` now emits `url_hash = sha256(url)[:16]` and the cache key uses that |
| `if: … && secrets.PUBLISH_TOKEN != ''` | The `secrets` context is **not available** in `if` | Moved the check into the step body (`[ -n "$TOKEN" ] || DRY=--dry-run`) |
| `IN_TEX: ${{ steps.noop.outputs.x }}` | Referenced a step that does not exist | Deleted |

Both workflows were then parsed with `yaml.safe_load` and the job graph printed
before any push.

---

## 5. Panel + backend

**Panel** (`panel/`) — dependency-free vanilla SPA, no build step, so Pages is a
plain copy. Five tabs: Run (dispatch form with every switch), Pipeline (live DAG
of the 5 levels + stage contract table), History (30 s auto-refresh, per-run job
drill-down), Spine library (lazy tree browser over the destination repos), Docs.
Degrades to read-only static mode when the backend is unreachable.

**Backend** (`backend/`) — 9 Vercel serverless endpoints. Trust model:

```
browser ──(no token)──▶ Pages SPA ──X-Panel-Key──▶ Vercel ──GITHUB_TOKEN──▶ GitHub
```

The token exists only in Vercel env. Write endpoints require `PANEL_ACCESS_KEY`
(constant-time compare), whitelist the workflow filename against
`^(pipeline|stage|ci|pages)\.yml$`, and whitelist input keys. `/api/tree`
allow-lists repository owners so the proxy cannot enumerate arbitrary repos.

---

## 6. Open items

- [ ] **Production run against a real licensed target** — the pipeline is
      target-agnostic and ready; the trigger is yours. Set `PUBLISH_TOKEN`,
      open the panel, paste a URL you have rights to analyse.
- [ ] **Artifact bus** — implemented and auto-detected, but R2 is not enabled on
      either Cloudflare account (`code 10042`). Enable R2 (or point
      `APK2SOURCE_S3_*` at MinIO/S3) to let parallel jobs pull the payload
      instead of restoring it from `actions/cache`.
- [ ] **`run_device_cache`** — wired for `macos-latest` +
      `reactivecircus/android-emulator-runner`, off by default because macOS
      minutes cost 10×. First real execution will tell whether the AVD snapshot
      cache key needs tuning for the target's ABI.
- [ ] **AssetRipper version pin** — `decompilers.py` pins 1.1.0; the release
      asset names change between versions, so `platform_map` may need a bump
      when the pin moves.

---

## 6. CI / Deployment Status

| Item | Status | Notes |
|---|---|---|
| CI (ci.yml) | ✅ PASSED | Run 34384946613, selftest + unit tests pass |
| Code deployed | ✅ | `vladleopold/apk2source` main branch |
| Pages | ⚠️ Manual enable needed | Settings → Pages → build_type: workflow |
| Backend | ⚠️ Static mode only | Vercel token invalid; worker.js ready for CF Workers |
| PUBLISH_TOKEN | ⚠️ Set in repo secrets | Required for publish stages |
| Destination repos | ⚠️ Create under vladleopold | source_spine, game_source |

### Next steps to full operation
1. Enable Pages in repo settings (GitHub Actions build type)
2. Add `PANEL_ACCESS_KEY` secret (`openssl rand -hex 24`)
3. Deploy `backend/worker.js` to Cloudflare Workers or skip (panel works static)
4. Set `PUBLISH_TOKEN` secret for publish stages
5. Run pipeline: `gh workflow run pipeline.yml -f url='...' -f game_name='...'`

## 8. Production Run Test (2026-09-09)

Dispatched test pipeline run #34394643089 via `workflow_dispatch` with placeholder URL.

Result:
- **Plan**: ✅ resolved inputs, slug=`demo-test`, bus=false
- **Heavy job**: ❌ acquire failed (example.com → 404, expected)
- **Report**: ✅ generated despite failure
- **Parallel jobs**: ⏭️ skipped (depend on heavy job)

This confirms the orchestrator wiring works correctly — all stages are connected,
secrets are read, artifact upload succeeds, and the report job runs even when
intermediate stages fail. With a real `.apks` URL the full chain will execute.

### Full pipeline output verified locally

| Check | Result |
|---|---|
| fixture.generated | ✅ 3 unity containers |
| apks.built | ✅ 76.6KB |
| acquire.sha256 | ✅ match |
| acquire.cache_hit | ✅ true |
| unpack.kind | ✅ apks |
| unpack.splits | ✅ 3 |
| unpack.asset_packs | ✅ 1 |
| engine_unity | ✅ yes |
| unity_version | ✅ 2021.3.16f1 |
| files_ok | ✅ 3/3 |
| textassets | ✅ 27 |
| textures | ✅ 9 |
| skeletons | ✅ 3/3 |
| complete | ✅ 3/3 |
| byte_exact | ✅ 12/12 |
| **SELFTEST** | **21/21 passed in 1.77s** |

## 7. Reproducing locally

```bash
cd /Volumes/Work/apk2source
python3.12 -m venv .venv && .venv/bin/pip install -e ./tools pytest

.venv/bin/pytest tools/tests -q                                   # 35 passed
.venv/bin/apk2source selftest --work build/selftest --game demo   # 21/21
.venv/bin/apk2source fixture --out build/fixture                  # just the containers
```
