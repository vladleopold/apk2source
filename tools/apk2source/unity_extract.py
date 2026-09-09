"""Stage 04 - UNITY ASSET DECOMPILATION.

Turns Unity payloads (APK `assets/bin/Data`, AssetBundles, Addressables, loose
`.assets`) into a normalised asset tree on disk:

    <out>/
      <container-or-file-stem>/
        TextAsset/<name>.<ext>
        Texture2D/<name>.png
        Sprite/<name>.png
        MonoBehaviour/<name>.json
        Mesh/<name>.obj
        AudioClip/<name>.<ext>
      _unity_report.json

Container paths from AssetBundle `m_Container` are preserved as directories,
which is what lets the Spine stage re-pair skeleton / atlas / page by folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .util import ensure_dir, human, log, read_json, safe_join, slug, warn, write_json

UNITY_FILE_SUFFIXES = {".assets", ".bundle", ".unity3d", ".resS", ".resource", ".ress",
                       ".split0", ".ab", ".bin", ".dat", ".pack", ".obb"}
UNITY_DIR_HINTS = ("assets/bin/Data", "assets/aa/", "assets/AddressableAssetsData",
                   "assets/StreamingAssets", "assets/AssetBundles", "assets/bundles")
MAGIC_PREFIXES = (b"UnityFS\x00", b"UnityWeb\x00", b"UnityRaw\x00", b"UnityArchive\x00")

TEXT_EXT_BY_CONTENT = (
    (b"{", ".json"),
    (b"<", ".xml"),
)


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def find_unity_files(root: Path, *, include_unknown_magic: bool = True,
                     limit: Optional[int] = None) -> List[Path]:
    root = Path(root)
    found: List[Path] = []
    seen: set = set()

    def add(p: Path) -> None:
        rp = p.resolve()
        if rp in seen or not p.is_file():
            return
        seen.add(rp)
        found.append(p)

    # 1. well-known Unity locations
    for hint in UNITY_DIR_HINTS:
        d = root / hint
        if d.exists():
            for p in d.rglob("*"):
                if p.is_file():
                    add(p)

    # 2. anything with a Unity-ish suffix
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix in UNITY_FILE_SUFFIXES or p.suffix.lower() in {s.lower() for s in UNITY_FILE_SUFFIXES}:
            add(p)
        elif p.name in ("globalgamemanagers", "level0", "level1", "mainData", "unity default resources",
                        "Resources/unity_builtin_extra"):
            add(p)

    # 3. magic sniff for extension-less / renamed blobs
    if include_unknown_magic:
        for p in root.rglob("*"):
            if not p.is_file() or p in seen:
                continue
            try:
                if p.stat().st_size < 16:
                    continue
                with open(p, "rb") as f:
                    head = f.read(16)
            except OSError:
                continue
            if head.startswith(MAGIC_PREFIXES):
                add(p)

    found.sort(key=lambda p: str(p))
    return found[:limit] if limit else found


# --------------------------------------------------------------------------
# per-file extraction (runs in a worker process)
# --------------------------------------------------------------------------
def _text_ext(name: str, data: bytes) -> str:
    low = name.lower()
    for suf in (".atlas.txt", ".atlas", ".json", ".xml", ".csv", ".txt", ".bytes", ".skel",
                ".skel.bytes", ".yaml", ".yml", ".glsl", ".shader", ".lua", ".proto"):
        if low.endswith(suf):
            return ""
    stripped = data.lstrip()[:1]
    if stripped == b"{":
        return ".json"
    if stripped == b"<":
        return ".xml"
    if b"\x00" in data[:4096]:
        return ".bytes"
    return ".txt"


def _unique(dirpath: Path, name: str) -> Path:
    p = dirpath / name
    if not p.exists():
        return p
    stem, suf = os.path.splitext(name)
    i = 1
    while True:
        cand = dirpath / f"{stem}__{i}{suf}"
        if not cand.exists():
            return cand
        i += 1


def _safe_name(name: str) -> str:
    name = (name or "unnamed").strip()
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    return name[:180] or "unnamed"


def extract_file(path_str: str, out_root: str, *, export_types: Tuple[str, ...],
                 keep_container_paths: bool = True, max_texture_side: int = 0) -> Dict[str, Any]:
    """Extract every asset from one Unity file. Import-safe for multiprocessing."""
    path = Path(path_str)
    out_root = Path(out_root)
    started = time.time()
    rec: Dict[str, Any] = {
        "file": str(path),
        "name": path.name,
        "size": path.stat().st_size if path.exists() else 0,
        "ok": False,
        "counts": {},
        "containers": 0,
        "unity_version": None,
        "errors": [],
    }
    try:
        import UnityPy
        from UnityPy.enums import ClassIDType
    except Exception as e:  # noqa: BLE001
        rec["errors"].append(f"UnityPy unavailable: {e}")
        return rec

    try:
        env = UnityPy.load(str(path))
    except Exception as e:  # noqa: BLE001
        rec["errors"].append(f"load failed: {type(e).__name__}: {e}")
        return rec

    stem = slug(path.name.split(".")[0]) or "file"
    base = ensure_dir(out_root / stem)
    counts: Dict[str, int] = {}

    def bump(k: str) -> None:
        counts[k] = counts.get(k, 0) + 1

    try:
        container_map: Dict[int, str] = {}
        if keep_container_paths:
            try:
                for cpath, obj in (env.container or {}).items():
                    pid = getattr(obj, "path_id", None)
                    if pid is not None:
                        container_map[pid] = cpath
            except Exception:  # noqa: BLE001
                pass
        rec["containers"] = len(container_map)
    except Exception:  # noqa: BLE001
        pass

    for obj in env.objects:
        tname = obj.type.name
        try:
            if export_types and tname not in export_types:
                continue
            if tname == "TextAsset":
                data = obj.read()
                name = _safe_name(getattr(data, "m_Name", "") or "textasset")
                script = getattr(data, "m_Script", b"")
                if isinstance(script, str):
                    raw = script.encode("utf-8", "surrogateescape")
                else:
                    raw = bytes(script)
                d = _target_dir(base, container_map, obj.path_id, "TextAsset", keep_container_paths)
                dest = _unique(d, name + _text_ext(name, raw))
                dest.write_bytes(raw)
                bump("TextAsset")
            elif tname == "Texture2D":
                data = obj.read()
                name = _safe_name(getattr(data, "m_Name", "") or "texture")
                img = data.image
                if img is None:
                    continue
                if max_texture_side and max(img.size) > max_texture_side:
                    img = img.copy()
                    img.thumbnail((max_texture_side, max_texture_side))
                d = _target_dir(base, container_map, obj.path_id, "Texture2D", keep_container_paths)
                dest = _unique(d, name + ".png")
                img.save(dest, "PNG")
                bump("Texture2D")
            elif tname == "Sprite":
                data = obj.read()
                name = _safe_name(getattr(data, "m_Name", "") or "sprite")
                img = getattr(data, "image", None)
                if img is None:
                    continue
                d = _target_dir(base, container_map, obj.path_id, "Sprite", keep_container_paths)
                dest = _unique(d, name + ".png")
                img.save(dest, "PNG")
                bump("Sprite")
            elif tname == "MonoBehaviour":
                try:
                    tree = obj.read_typetree()
                except Exception:  # noqa: BLE001
                    continue
                name = _safe_name(str(tree.get("m_Name") or f"monobehaviour_{obj.path_id}"))
                d = _target_dir(base, container_map, obj.path_id, "MonoBehaviour", keep_container_paths)
                dest = _unique(d, name + ".json")
                dest.write_text(json.dumps(tree, indent=2, default=str), encoding="utf-8")
                bump("MonoBehaviour")
            elif tname == "Mesh":
                data = obj.read()
                name = _safe_name(getattr(data, "m_Name", "") or "mesh")
                try:
                    txt = data.export()
                except Exception:  # noqa: BLE001
                    continue
                if not txt:
                    continue
                d = _target_dir(base, container_map, obj.path_id, "Mesh", keep_container_paths)
                dest = _unique(d, name + ".obj")
                dest.write_text(txt if isinstance(txt, str) else txt.decode("utf-8", "replace"),
                                encoding="utf-8")
                bump("Mesh")
            elif tname == "AudioClip":
                data = obj.read()
                name = _safe_name(getattr(data, "m_Name", "") or "audioclip")
                samples = getattr(data, "samples", {}) or {}
                d = _target_dir(base, container_map, obj.path_id, "AudioClip", keep_container_paths)
                for sname, sdata in samples.items():
                    dest = _unique(d, _safe_name(sname))
                    dest.write_bytes(sdata if isinstance(sdata, bytes) else bytes(sdata))
                    bump("AudioClip")
            elif tname == "AssetBundle":
                bump("AssetBundle")
            elif tname == "VideoClip":
                data = obj.read()
                name = _safe_name(getattr(data, "m_Name", "") or "videoclip")
                raw = getattr(data, "m_VideoData", None) or getattr(data, "m_ExternalResources", None)
                if isinstance(raw, (bytes, bytearray)) and raw:
                    d = _target_dir(base, container_map, obj.path_id, "VideoClip", keep_container_paths)
                    _unique(d, name + ".mp4").write_bytes(bytes(raw))
                    bump("VideoClip")
            elif tname == "Font":
                bump("Font")
            elif tname == "Shader":
                bump("Shader")
            elif tname == "Material":
                bump("Material")
            elif tname == "AnimationClip":
                bump("AnimationClip")
            else:
                bump(f"other:{tname}")
        except Exception as e:  # noqa: BLE001
            rec["errors"].append(f"{tname}#{getattr(obj,'path_id','?')}: {type(e).__name__}: {e}")
            if len(rec["errors"]) > 200:
                break

    try:
        for f in getattr(env, "files", {}).values():
            v = getattr(f, "unity_version", None)
            if v:
                rec["unity_version"] = str(v)
                break
    except Exception:  # noqa: BLE001
        pass

    rec["ok"] = bool(counts) or not rec["errors"]
    rec["counts"] = counts
    rec["seconds"] = round(time.time() - started, 2)
    rec["out"] = str(base)
    rec["errors"] = rec["errors"][:50]
    return rec


def _target_dir(base: Path, container_map: Dict[int, str], path_id: int,
                kind: str, keep_container_paths: bool) -> Path:
    if keep_container_paths and path_id in container_map:
        cpath = container_map[path_id]
        parts = [slug(x) for x in cpath.split("/") if x not in ("", ".")]
        if parts:
            if parts[-1].lower().endswith((".png", ".json", ".txt", ".atlas", ".bytes", ".prefab")):
                parts = parts[:-1]
            return ensure_dir(base.joinpath(*parts[-4:]))
    return ensure_dir(base / kind)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
DEFAULT_EXPORT_TYPES = ("TextAsset", "Texture2D", "Sprite", "MonoBehaviour",
                        "Mesh", "AudioClip", "AssetBundle", "VideoClip")


def extract_all(root: Path, out_dir: Path, *, jobs: int = 0,
                export_types: Iterable[str] = DEFAULT_EXPORT_TYPES,
                keep_container_paths: bool = True,
                limit: Optional[int] = None,
                max_texture_side: int = 0) -> Dict[str, Any]:
    root, out_dir = Path(root), ensure_dir(out_dir)
    files = find_unity_files(root, limit=limit)
    log(f"unity files discovered: {len(files)}")
    types = tuple(export_types)
    started = time.time()
    results: List[Dict[str, Any]] = []

    jobs = jobs or max(1, min(8, (os.cpu_count() or 2)))
    if len(files) <= 1 or jobs == 1:
        for f in files:
            results.append(extract_file(str(f), str(out_dir), export_types=types,
                                        keep_container_paths=keep_container_paths,
                                        max_texture_side=max_texture_side))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {
                ex.submit(extract_file, str(f), str(out_dir), export_types=types,
                          keep_container_paths=keep_container_paths,
                          max_texture_side=max_texture_side): f
                for f in files
            }
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    results.append(fut.result())
                except Exception as e:  # noqa: BLE001
                    results.append({"file": str(futs[fut]), "ok": False,
                                    "errors": [f"worker crashed: {type(e).__name__}: {e}"]})
                if i % 25 == 0 or i == len(futs):
                    log(f"  extracted {i}/{len(futs)} unity files")

    totals: Dict[str, int] = {}
    ok = failed = 0
    for r in results:
        ok += bool(r.get("ok"))
        failed += not r.get("ok")
        for k, v in (r.get("counts") or {}).items():
            totals[k] = totals.get(k, 0) + v
    versions = sorted({r["unity_version"] for r in results if r.get("unity_version")})

    report = {
        "stage": "unity_extract",
        "root": str(root),
        "out": str(out_dir),
        "files_total": len(files),
        "files_ok": ok,
        "files_failed": failed,
        "unity_versions": versions,
        "totals": totals,
        "seconds": round(time.time() - started, 2),
        "results": results,
    }
    write_json(out_dir / "_unity_report.json", report)
    log(f"unity extract done: {ok}/{len(files)} files ok, totals={totals}, "
        f"versions={versions}, {report['seconds']}s")
    return report
