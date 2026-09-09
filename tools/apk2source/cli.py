#!/usr/bin/env python3
"""apk2source command line interface.

One entry point for every pipeline stage, so a GitHub Actions step and a local
debug session run exactly the same code path.

    apk2source acquire       --url URL --out DIR [--cache-dir DIR] [--no-cache]
    apk2source unpack        --input FILE --out DIR
    apk2source detect        --merged DIR --work DIR
    apk2source device-cache  --splits DIR --out DIR [--package PKG]
    apk2source extract-unity --root DIR --out DIR [--jobs N]
    apk2source extract-spine --assets DIR --out DIR --game NAME
    apk2source publish       --src DIR --repo owner/name --subdir PATH
    apk2source report        --work DIR --game NAME
    apk2source selftest      --work DIR          # full chain on a generated fixture
    apk2source bus-put|bus-get|bus-ls|bus-rm
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import acquire as acquire_mod
from . import bus as bus_mod
from . import containers, decompilers, device, engine, gitpub, report, spine, unity_extract
from .util import (dir_size, ensure_dir, gh_output, human, log, read_json, sha256_file,
                   slug, warn, write_json)


# --------------------------------------------------------------------------
def cmd_acquire(a: argparse.Namespace) -> int:
    urls = [u for u in (a.url or []) if u]
    if a.url_file:
        urls += [ln.strip() for ln in Path(a.url_file).read_text().splitlines() if ln.strip()]
    if not urls:
        raise SystemExit("no url given")
    rec = acquire_mod.acquire(
        urls, Path(a.out), Path(a.cache_dir) if a.cache_dir else None,
        use_cache=not a.no_cache, sha256=a.sha256, filename=a.filename, timeout=a.timeout,
    )
    if a.work:
        report.save_stage(Path(a.work), "acquire", rec)
    gh_output("payload_path", rec["path"])
    gh_output("payload_sha256", rec["sha256"])
    gh_output("payload_size", str(rec["size"]))
    gh_output("payload_cached", str(rec["cached"]).lower())
    return 0


def cmd_unpack(a: argparse.Namespace) -> int:
    rec = containers.unpack(Path(a.input), Path(a.out), keep_splits=not a.drop_splits)
    if a.work:
        report.save_stage(Path(a.work), "unpack", rec)
    gh_output("merged_dir", rec["merged"])
    gh_output("splits_dir", rec.get("splits_dir") or "")
    gh_output("package", rec.get("package") or "")
    gh_output("container_kind", rec["kind"])
    return 0


def cmd_detect(a: argparse.Namespace) -> int:
    rec = engine.detect(Path(a.merged))
    out = Path(a.work) if a.work else Path(a.merged).parent
    report.save_stage(out, "engine_detect", rec)
    write_json(Path(a.out) if a.out else out / "engine.json", rec)
    gh_output("engine", rec["engine"])
    gh_output("unity_version", rec.get("unity_version") or "")
    gh_output("spine_likely", str(rec["spine_signals"]["likely"]).lower())
    return 0


def cmd_device_cache(a: argparse.Namespace) -> int:
    rec = device.capture(Path(a.splits), Path(a.out), package=a.package, serial=a.serial,
                         settle_seconds=a.settle, pull_cache=not a.no_pull,
                         skip_install=a.skip_install)
    if a.work:
        report.save_stage(Path(a.work), "device_cache", rec)
    gh_output("cache_dir", rec.get("cache_dir") or "")
    gh_output("cache_files", str(rec.get("cache_files", 0)))
    gh_output("cache_bytes", str(rec.get("cache_bytes", 0)))
    gh_output("device_ok", str(rec.get("ok", False)).lower())
    return 0 if rec.get("ok") or rec.get("degraded") else 1


def cmd_extract_unity(a: argparse.Namespace) -> int:
    roots = [Path(r) for r in a.root]
    recs = []
    for r in roots:
        if not r.exists():
            warn(f"root missing: {r}")
            continue
        recs.append(unity_extract.extract_all(
            r, Path(a.out), jobs=a.jobs,
            export_types=tuple(a.types) if a.types else unity_extract.DEFAULT_EXPORT_TYPES,
            keep_container_paths=not a.flat, limit=a.limit, max_texture_side=a.max_texture_side))
    merged: Dict[str, Any] = {"stage": "unity_extract", "ok": bool(recs), "parts": recs,
                              "files_total": sum(r["files_total"] for r in recs),
                              "files_ok": sum(r["files_ok"] for r in recs),
                              "totals": {}, "unity_versions": [], "out": a.out}
    for r in recs:
        for k, v in r["totals"].items():
            merged["totals"][k] = merged["totals"].get(k, 0) + v
        merged["unity_versions"] += [v for v in r["unity_versions"] if v not in merged["unity_versions"]]
    if a.work:
        report.save_stage(Path(a.work), "unity_extract", merged)
    gh_output("assets_dir", str(Path(a.out)))
    gh_output("unity_files_ok", str(merged["files_ok"]))
    return 0


def cmd_extract_spine(a: argparse.Namespace) -> int:
    rec = spine.run(Path(a.assets), Path(a.out), a.game,
                    provenance={"package": a.package, "run_id": os.environ.get("GITHUB_RUN_ID", "")})
    if a.work:
        report.save_stage(Path(a.work), "spine_extract", rec)
    gh_output("spine_dir", rec["out"])
    gh_output("spine_skeletons", str(rec["stats"]["skeletons"]))
    gh_output("spine_complete", str(rec["stats"]["complete"]))
    gh_output("spine_textures", str(rec["stats"]["textures"]))
    return 0 if rec["stats"]["skeletons"] else 2


def cmd_publish(a: argparse.Namespace) -> int:
    token = a.token or os.environ.get("APK2SOURCE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    rec = gitpub.publish(Path(a.src), repo=a.repo, subdir=a.subdir or "", token=token,
                         branch=a.branch, message=a.message, workdir=Path(a.workdir) if a.workdir else None,
                         use_lfs=not a.no_lfs, only_exts=tuple(a.only) if a.only else None,
                         mode=a.mode, dry_run=a.dry_run, base_branch=a.base_branch)
    if a.work:
        report.save_stage(Path(a.work), "publish", rec)
    gh_output("published", str(bool(rec.get("pushed"))).lower())
    gh_output("published_files", str((rec.get("copy") or {}).get("copied", 0)))
    return 0 if rec.get("pushed") or rec.get("dry_run") else 1


def cmd_survey(a: argparse.Namespace) -> int:
    s = decompilers.survey(Path(a.merged))
    write_json(Path(a.out) if a.out else Path(a.merged).parent / "decompile_survey.json", s)
    print(json.dumps({k: v for k, v in s.items() if k.startswith("can_")}, indent=2))
    return 0


def cmd_decompile(a: argparse.Namespace) -> int:
    merged = Path(a.merged)
    out = Path(a.out)
    ensure_dir(out)
    results: Dict[str, Any] = {}
    targets = a.tools or ["apktool", "jadx", "assetripper", "il2cppdumper"]
    for t in targets:
        log(f"--- {t} ---")
        if t == "apktool":
            apk = next(iter(merged.rglob("*.apk")), None) or a.apk
            results[t] = decompilers.apktool(Path(apk), out / "apktool") if apk else {"ok": False, "error": "no apk"}
        elif t == "jadx":
            apk = next(iter(merged.rglob("*.apk")), None) or a.apk
            results[t] = decompilers.jadx(Path(apk), out / "jadx") if apk else {"ok": False, "error": "no apk"}
        elif t == "assetripper":
            results[t] = decompilers.assetripper(Path(a.assets), out / "ExportedProject")
        elif t == "il2cppdumper":
            results[t] = decompilers.il2cppdumper(merged, out / "il2cpp")
    write_json(out / "decompile.json", results)
    if a.work:
        report.save_stage(Path(a.work), "decompile", {"stage": "decompile", "ok": True, "tools": results})
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    rep = report.emit(Path(a.work), a.game, a.run_id)
    print(json.dumps(rep["summary"], indent=2))
    gh_output("skeletons", str(rep["summary"]["skeletons"]))
    gh_output("engine", str(rep["summary"]["engine"]))
    if a.out:
        write_json(a.out, rep)
    return 0


# --------------------------------------------------------------------------
# bus
# --------------------------------------------------------------------------
def _bus(a: argparse.Namespace) -> bus_mod.Bus:
    return bus_mod.from_env(getattr(a, "backend", None))


def cmd_bus_put(a: argparse.Namespace) -> int:
    rec = _bus(a).put(Path(a.src), a.key)
    print(json.dumps(rec, indent=2))
    gh_output("bus_key", a.key)
    return 0


def cmd_bus_get(a: argparse.Namespace) -> int:
    p = _bus(a).get(a.key, Path(a.dest))
    print(json.dumps({"dest": str(p), "bytes": p.stat().st_size}))
    gh_output("payload_path", str(p))
    return 0


def cmd_bus_ls(a: argparse.Namespace) -> int:
    print(json.dumps(_bus(a).list(a.prefix), indent=2))
    return 0


def cmd_bus_rm(a: argparse.Namespace) -> int:
    print(json.dumps({"deleted": _bus(a).delete(a.key)}))
    return 0


# --------------------------------------------------------------------------
# selftest - the end-to-end proof
# --------------------------------------------------------------------------
def cmd_fixture(a: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "fixtures"))
    import make_fixture  # type: ignore

    man = make_fixture.build_all(a.out, a.unity)
    write_json(Path(a.out) / "fixture_manifest.json", man)
    print(json.dumps({"out": a.out, "files": man["files"], "skeletons": man["skeletons"]}, indent=2))
    return 0


def cmd_selftest(a: argparse.Namespace) -> int:
    """Run the whole chain against a generated fixture and verify every byte."""
    import hashlib
    import zipfile

    work = ensure_dir(Path(a.work))
    t0 = time.time()
    results: Dict[str, Any] = {"checks": [], "ok": True}

    def check(name: str, passed: bool, detail: str = "") -> None:
        results["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
        results["ok"] = results["ok"] and bool(passed)
        log(f"[{'PASS' if passed else 'FAIL'}] {name} {detail}")

    # 1. fixture -----------------------------------------------------------
    fixture_dir = work / "fixture"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "fixtures"))
    import make_fixture  # type: ignore

    man = make_fixture.build_all(str(fixture_dir), a.unity)
    check("fixture.generated", all((fixture_dir / f).exists() for f in man["files"]),
          f"{len(man['files'])} unity containers")

    # 2. synthetic split-APK set (.apks) -----------------------------------
    apks_dir = ensure_dir(work / "apks_in")
    splits_dir = ensure_dir(work / "splits_build")
    base_apk = splits_dir / "base.apk"
    with zipfile.ZipFile(base_apk, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AndroidManifest.xml", _fake_axml("com.apk2source.selftest"))
        z.writestr("assets/bin/Data/sharedassets0.assets",
                   (fixture_dir / "sharedassets0.assets").read_bytes())
        z.writestr("classes.dex", b"dex\n035\x00" + b"\x00" * 64)
        z.writestr("lib/arm64-v8a/libil2cpp.so", b"\x7fELF" + b"\x00" * 64)
        z.writestr("lib/arm64-v8a/libunity.so", b"\x7fELF" + b"\x00" * 64)
    pack_apk = splits_dir / "split_ggpack1.apk"
    with zipfile.ZipFile(pack_apk, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("assets/aa/Android/spine_assets.bundle",
                   (fixture_dir / "spine_assets.bundle").read_bytes())
        z.writestr("assets/aa/Android/spine_assets_lz4.bundle",
                   (fixture_dir / "spine_assets_lz4.bundle").read_bytes())
    cfg_apk = splits_dir / "split_config.arm64_v8a.apk"
    with zipfile.ZipFile(cfg_apk, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("lib/arm64-v8a/libmain.so", b"\x7fELF" + b"\x00" * 32)

    apks = apks_dir / "selftest.apks"
    with zipfile.ZipFile(apks, "w", zipfile.ZIP_STORED) as z:
        z.write(base_apk, "base.apk")
        z.write(cfg_apk, "split_config.arm64_v8a.apk")
        z.write(pack_apk, "split_ggpack1.apk")
        z.writestr("meta.sai_v2.json", json.dumps({"name": "selftest", "versionName": "1.0.0"}))
    check("apks.built", apks.exists(), human(apks.stat().st_size))

    # 3. acquire (file:// through the cache path) --------------------------
    acq = acquire_mod.acquire([apks.resolve().as_uri()], work / "payload", work / "cache", use_cache=True)
    check("acquire.sha256", acq["sha256"] == sha256_file(apks), acq["sha256"][:16])
    acq2 = acquire_mod.acquire([apks.resolve().as_uri()], work / "payload2", work / "cache", use_cache=True)
    check("acquire.cache_hit", acq2["cached"] is True)
    report.save_stage(work, "acquire", acq)

    # 4. unpack ------------------------------------------------------------
    up = containers.unpack(Path(acq["path"]), work / "unpacked")
    report.save_stage(work, "unpack", up)
    check("unpack.kind", up["kind"] == "apks", up["kind"])
    check("unpack.splits", len(up["splits"]) == 3, str(len(up["splits"])))
    check("unpack.asset_packs", len(up["asset_packs"]) == 1, str(len(up["asset_packs"])))
    merged = Path(up["merged"])
    check("unpack.merged_unity", (merged / "assets/bin/Data/sharedassets0.assets").exists()
          and (merged / "assets/aa/Android/spine_assets.bundle").exists())

    # 5. engine detection ----------------------------------------------------
    eng = engine.detect(merged)
    report.save_stage(work, "engine_detect", eng)
    check("detect.engine_unity", eng["engine"] == "unity", eng["engine"])
    check("detect.unity_version", eng["unity_version"] == a.unity, str(eng["unity_version"]))
    check("detect.il2cpp", "libil2cpp.so" in eng["native_libs"])

    # 6. unity extraction ----------------------------------------------------
    uni = unity_extract.extract_all(merged, work / "assets", jobs=1)
    report.save_stage(work, "unity_extract", uni)
    check("unity.files_ok", uni["files_ok"] == uni["files_total"] and uni["files_total"] >= 3,
          f"{uni['files_ok']}/{uni['files_total']}")
    check("unity.textassets", uni["totals"].get("TextAsset", 0) >= 9, str(uni["totals"].get("TextAsset")))
    check("unity.textures", uni["totals"].get("Texture2D", 0) >= 3, str(uni["totals"].get("Texture2D")))

    # 7. spine extraction ----------------------------------------------------
    sp = spine.run(work / "assets", work / "spine", a.game)
    report.save_stage(work, "spine_extract", sp)
    stats = sp["stats"]
    n = len(man["expect"])
    check("spine.skeletons", stats["skeletons"] == n, f"{stats['skeletons']}/{n}")
    check("spine.complete", stats["complete"] == n, f"{stats['complete']}/{n}")
    check("spine.json", stats["json"] == n, str(stats["json"]))
    check("spine.atlas", stats["atlas"] == n, str(stats["atlas"]))
    check("spine.textures", stats["textures"] == n, str(stats["textures"]))

    # 8. byte-exact verification --------------------------------------------
    root = Path(sp["out"])
    exact = 0
    for name, exp in man["expect"].items():
        d = root / slug(name)
        got = {f["role"]: f for f in (read_json(d / "_meta.json", {}) or {}).get("files", [])}
        for role, key in (("skeleton_json", "json_sha256"), ("atlas", "atlas_sha256"),
                          ("skeleton_binary", "skel_sha256")):
            f = got.get(role)
            if not f:
                continue
            h = sha256_file(d / f["name"])
            if h == exp[key]:
                exact += 1
            else:
                check(f"spine.bytes.{name}.{role}", False, f"{h[:12]} != {exp[key][:12]}")
        tex = got.get("texture")
        if tex:
            # Unity stores textures bottom-up; UnityPy flips them on export,
            # so the expected digest is the vertically flipped raw RGBA.
            want = exp["rgba_sha256_flipped"]
            try:
                from PIL import Image
                img = Image.open(d / tex["name"]).convert("RGBA")
                h = hashlib.sha256(img.tobytes()).hexdigest()
            except Exception as e:  # noqa: BLE001
                h = f"error:{e}"
            if h == want:
                exact += 1
            else:
                check(f"spine.bytes.{name}.texture", False, f"{str(h)[:12]} != {want[:12]}")
    expected_exact = n * 4
    check("spine.byte_exact", exact == expected_exact, f"{exact}/{expected_exact}")

    # 9. report --------------------------------------------------------------
    rep = report.emit(work, a.game, run_id="selftest")
    check("report.summary", rep["summary"]["skeletons"] == n, str(rep["summary"]["skeletons"]))

    results["seconds"] = round(time.time() - t0, 2)
    results["passed"] = sum(1 for c in results["checks"] if c["passed"])
    results["total"] = len(results["checks"])
    write_json(work / "selftest.json", results)
    log(f"SELFTEST {results['passed']}/{results['total']} passed in {results['seconds']}s")
    gh_output("selftest_passed", str(results["passed"]))
    gh_output("selftest_total", str(results["total"]))
    gh_output("selftest_ok", str(results["ok"]).lower())
    from .util import gh_summary

    gh_summary(report.markdown(rep))
    return 0 if results["ok"] else 1


def _fake_axml(package: str) -> bytes:
    """Minimal binary AndroidManifest.xml with a UTF-8 string pool.

    Real enough for `containers.parse_axml_strings` to recover the package id
    during the selftest, without shipping a signed manifest.
    """
    import struct

    strings = [package, "manifest", "versionName", "1.0.0", "android", "application"]
    encoded = bytearray()
    offsets: List[int] = []
    for s_ in strings:
        offsets.append(len(encoded))
        b = s_.encode("utf-8")
        encoded.append(len(s_) & 0x7F)      # utf16 length (short form)
        encoded.append(len(b) & 0x7F)       # utf8 byte length (short form)
        encoded += b
        encoded.append(0)
    encoded.append(0)

    header_len = 28 + 4 * len(strings)
    chunk_size = header_len + len(encoded)
    chunk = struct.pack("<HHI", 0x0001, 28, chunk_size)
    chunk += struct.pack("<IIIII", len(strings), 0, 1 << 8, header_len, 0)
    chunk += b"".join(struct.pack("<I", o) for o in offsets)
    chunk += bytes(encoded)

    doc = b"\x03\x00\x08\x00" + struct.pack("<I", 8 + len(chunk)) + chunk
    return doc


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="apk2source", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--work", help="pipeline state dir (stage json + pipeline.json)")

    s = sub.add_parser("acquire"); common(s)
    s.add_argument("--url", action="append"); s.add_argument("--url-file")
    s.add_argument("--out", required=True); s.add_argument("--cache-dir")
    s.add_argument("--no-cache", action="store_true"); s.add_argument("--sha256")
    s.add_argument("--filename"); s.add_argument("--timeout", type=int, default=120)
    s.set_defaults(fn=cmd_acquire)

    s = sub.add_parser("unpack"); common(s)
    s.add_argument("--input", required=True); s.add_argument("--out", required=True)
    s.add_argument("--drop-splits", action="store_true")
    s.set_defaults(fn=cmd_unpack)

    s = sub.add_parser("detect"); common(s)
    s.add_argument("--merged", required=True); s.add_argument("--out")
    s.set_defaults(fn=cmd_detect)

    s = sub.add_parser("device-cache"); common(s)
    s.add_argument("--splits", required=True); s.add_argument("--out", required=True)
    s.add_argument("--package"); s.add_argument("--serial")
    s.add_argument("--settle", type=int, default=900)
    s.add_argument("--no-pull", action="store_true"); s.add_argument("--skip-install", action="store_true")
    s.set_defaults(fn=cmd_device_cache)

    s = sub.add_parser("extract-unity"); common(s)
    s.add_argument("--root", action="append", required=True); s.add_argument("--out", required=True)
    s.add_argument("--jobs", type=int, default=0); s.add_argument("--types", action="append")
    s.add_argument("--flat", action="store_true"); s.add_argument("--limit", type=int)
    s.add_argument("--max-texture-side", type=int, default=0)
    s.set_defaults(fn=cmd_extract_unity)

    s = sub.add_parser("extract-spine"); common(s)
    s.add_argument("--assets", required=True); s.add_argument("--out", required=True)
    s.add_argument("--game", required=True); s.add_argument("--package")
    s.set_defaults(fn=cmd_extract_spine)

    s = sub.add_parser("publish"); common(s)
    s.add_argument("--src", required=True); s.add_argument("--repo", required=True)
    s.add_argument("--subdir", default=""); s.add_argument("--token")
    s.add_argument("--branch", default="main"); s.add_argument("--base-branch", default="main")
    s.add_argument("--message", default="apk2source: update"); s.add_argument("--workdir")
    s.add_argument("--no-lfs", action="store_true"); s.add_argument("--only", action="append")
    s.add_argument("--mode", choices=("direct", "pr"), default="direct")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_publish)

    s = sub.add_parser("survey")
    s.add_argument("--merged", required=True); s.add_argument("--out")
    s.set_defaults(fn=cmd_survey)

    s = sub.add_parser("decompile"); common(s)
    s.add_argument("--merged", required=True); s.add_argument("--assets", required=True)
    s.add_argument("--out", required=True); s.add_argument("--apk")
    s.add_argument("--tools", action="append")
    s.set_defaults(fn=cmd_decompile)

    s = sub.add_parser("report")
    s.add_argument("--work", required=True); s.add_argument("--game", default="")
    s.add_argument("--run-id", default=""); s.add_argument("--out")
    s.set_defaults(fn=cmd_report)

    for name, fn in (("bus-put", cmd_bus_put), ("bus-get", cmd_bus_get),
                     ("bus-ls", cmd_bus_ls), ("bus-rm", cmd_bus_rm)):
        s = sub.add_parser(name)
        s.add_argument("--backend")
        if name == "bus-put":
            s.add_argument("--src", required=True); s.add_argument("--key", required=True)
        elif name == "bus-get":
            s.add_argument("--key", required=True); s.add_argument("--dest", required=True)
        elif name == "bus-ls":
            s.add_argument("--prefix", default="")
        else:
            s.add_argument("--key", required=True)
        s.set_defaults(fn=fn)

    s = sub.add_parser("fixture")
    s.add_argument("--out", required=True); s.add_argument("--unity", default="2021.3.16f1")
    s.set_defaults(fn=cmd_fixture)

    s = sub.add_parser("selftest")
    s.add_argument("--work", required=True); s.add_argument("--game", default="apk2source-selftest")
    s.add_argument("--unity", default="2021.3.16f1")
    s.set_defaults(fn=cmd_selftest)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.time()
    rc = args.fn(args)
    log(f"`{args.cmd}` finished rc={rc} in {round(time.time()-started,1)}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
