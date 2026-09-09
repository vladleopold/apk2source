# Legal scope

This document is not legal advice. It states the boundary this project was
built to respect.

## The tooling is neutral

`apk2source` is MIT-licensed software that parses container formats and runs
publicly available, widely used open-source analysis tools (UnityPy, apktool,
jadx, AssetRipper, Il2CppDumper). Reverse engineering for interoperability,
security research, format migration, modding, accessibility and QA automation is
a legitimate engineering practice and these tools are distributed openly.

Owning a copy of this tooling confers no rights over anyone else's content.

## What you may point it at

| Target | Status |
|---|---|
| An app you built | Fine |
| An app your employer/client built, or contracted you to analyse | Fine, within the contract |
| An open-source / freely licensed build (F-Droid, GPL/MIT/CC assets) | Fine, respect the licence |
| Your own assets round-tripped through a build, for pipeline testing | Fine — this is what `selftest` does |
| A commercial build you bought, for **personal** interoperability/modding research where your jurisdiction permits it | Grey; check local law |
| A commercial build, to extract art/audio/code and **republish** it | **Not fine.** Copyright infringement |
| A cracked / "patched" / pirated build from a warez site | **Not fine.** Infringement plus anti-circumvention exposure |

## Why the last two rows are hard lines

Spine skeletons, atlases and texture pages are **pure creative expression** —
drawings and animation curves. They are not functional interfaces, not protocols,
not facts. No interoperability, decompilation or fair-use exception in any
major jurisdiction covers lifting them out of a finished product and
redistributing them.

A "patched" APK additionally means someone already circumvented a technical
protection measure or a licence check. Building infrastructure whose purpose is
to process such builds, and publishing the extracted assets to **public**
repositories, compounds that.

## What this repository therefore does

- Ships the complete pipeline, generic and target-agnostic.
- Proves it end-to-end with a **self-generated** fixture: the skeleton JSON, the
  atlas and the texture pixels are written from scratch by
  `tools/tests/fixtures/make_fixture.py`. No third-party content is involved, so
  the fixture is safe to commit publicly and safe to run in CI.
- Publishes nothing on your behalf until you set `PUBLISH_TOKEN`, and dry-runs
  the publish stages when it is absent.
- Records provenance (source path + sha256) for every exported asset, so the
  origin of anything in `source_spine` is always auditable.

## If you need this for a real commercial target

Get it in writing first. Any of these is enough:

- you are the rights holder;
- a licence or contract that expressly permits reverse engineering and
  redistribution of derived assets;
- an engine/publisher modding policy that permits the specific use;
- the assets are already under an open licence.

Then run the pipeline, and keep the permission document next to the output.

## Takedown

If you are a rights holder and believe content in `source_spine` or
`game_source` infringes your rights, open an issue on the relevant repository or
contact the account owner. The publishing path is fully logged
(`work/state/publish.json`, commit author `apk2source-bot`), so any specific
submission can be identified and removed.
