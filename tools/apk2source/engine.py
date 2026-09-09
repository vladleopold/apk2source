"""Stage 02b - ENGINE DETECTION.

Fingerprints the merged APK tree and decides which decompiler chain to run.
Detection is evidence-based (file presence + magic bytes), never name-based
guessing, so obfuscated/repacked builds still classify correctly.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .util import log, write_json

UNITYFS_MAGIC = b"UnityFS\x00"
UNITYWEB_MAGIC = b"UnityWeb\x00"
UNITYRAW_MAGIC = b"UnityRaw\x00"
UNITYARCHIVE_MAGIC = b"UnityArchive\x00"
OGG_MAGIC = b"OggS"
ELF_MAGIC = b"\x7fELF"
DEX_MAGIC = b"dex\n"
ZIP_MAGIC = b"PK\x03\x04"
WEBP_MAGIC = b"RIFF"
PNG_MAGIC = b"\x89PNG"
SPINE_SKEL_CANDIDATES = (b"\x00\x00\x00\x00\x01\x00", b"SPINEBIN")


class Engine:
    UNITY = "unity"
    UNREAL = "unreal"
    GODOT = "godot"
    LIBGDX = "libgdx"
    COCOS = "cocos"
    DEFOLD = "defold"
    CORONA = "corona"
    NATIVE = "native"
    UNKNOWN = "unknown"


def _head(p: Path, n: int = 16) -> bytes:
    try:
        with open(p, "rb") as f:
            return f.read(n)
    except Exception:  # noqa: BLE001
        return b""


def _so_names(root: Path) -> List[str]:
    return [p.name for p in root.rglob("lib/**/*.so")] + [p.name for p in root.rglob("*.so")]


def detect(root: Path) -> Dict[str, Any]:
    """Return an engine fingerprint report for a merged APK tree."""
    root = Path(root)
    evidence: Dict[str, List[str]] = {}
    files = [p for p in root.rglob("*") if p.is_file()]
    rel = {p.relative_to(root).as_posix(): p for p in files}

    def add(engine: str, note: str) -> None:
        evidence.setdefault(engine, []).append(note)

    # ---- Unity -----------------------------------------------------------
    unity_hits = 0
    if "assets/bin/Data/data.unity3d" in rel:
        add(Engine.UNITY, "assets/bin/Data/data.unity3d")
        unity_hits += 3
    if "assets/bin/Data/globalgamemanagers" in rel:
        add(Engine.UNITY, "assets/bin/Data/globalgamemanagers")
        unity_hits += 3
    for k in rel:
        if k.startswith("assets/bin/Data/") and (k.endswith(".assets") or k.endswith(".resS")
                                                 or k.endswith(".resource")):
            add(Engine.UNITY, k)
            unity_hits += 2
        if k.startswith("assets/aa/") or k.startswith("assets/AddressableAssetsData/"):
            add(Engine.UNITY, f"addressables: {k}")
            unity_hits += 2
    sos = _so_names(root)
    if "libil2cpp.so" in sos:
        add(Engine.UNITY, "libil2cpp.so (IL2CPP build)")
        unity_hits += 3
    if "libunity.so" in sos:
        add(Engine.UNITY, "libunity.so")
        unity_hits += 3
    if any(re.search(r"\.bundle$", k) for k in rel):
        n = sum(1 for k in rel if k.endswith(".bundle"))
        add(Engine.UNITY, f"{n} x *.bundle")
        unity_hits += 1
    # magic sniff on unlabelled blobs
    for k, p in list(rel.items())[:4000]:
        if p.stat().st_size < 8:
            continue
        h = _head(p, 16)
        if h.startswith((UNITYFS_MAGIC, UNITYWEB_MAGIC, UNITYRAW_MAGIC, UNITYARCHIVE_MAGIC)):
            add(Engine.UNITY, f"unity magic: {k}")
            unity_hits += 2
            if len(evidence[Engine.UNITY]) > 40:
                break

    # ---- Unreal ----------------------------------------------------------
    if any(k.startswith("assets/UnrealGame/") or "/Paks/" in k or k.endswith(".pak") for k in rel):
        add(Engine.UNREAL, "UnrealGame/Paks/*.pak")
    if "libUnreal.so" in sos or "libUE4.so" in sos:
        add(Engine.UNREAL, "libUnreal.so/libUE4.so")

    # ---- Godot -----------------------------------------------------------
    if any(k.endswith(".pck") for k in rel) or "libgodot_android.so" in sos:
        add(Engine.GODOT, "*.pck / libgodot_android.so")

    # ---- libGDX ----------------------------------------------------------
    if "libgdx.so" in sos or "libgdx-box2d.so" in sos:
        add(Engine.LIBGDX, "libgdx*.so")
    if any(k.startswith("assets/") and k.endswith(".atlas") for k in rel):
        add(Engine.LIBGDX, "loose *.atlas in assets/")

    # ---- Cocos / Defold / Corona ----------------------------------------
    if any(k.startswith("assets/src/") and k.endswith(".jsc") for k in rel) or "libcocos2djs.so" in sos:
        add(Engine.COCOS, "cocos2d-js")
    if any(k.endswith(".dmanifest") or k.endswith(".luac") for k in rel):
        add(Engine.DEFOLD, "*.dmanifest/*.luac")
    if "assets/main.lua" in rel or "libcorona.so" in sos:
        add(Engine.CORONA, "corona/solar2d")

    # ---- runtime / scripting layer --------------------------------------
    dex = [k for k in rel if k.endswith(".dex")]
    arsc = "resources.arsc" in rel
    native = bool(sos)

    scores = {e: len(v) for e, v in evidence.items()}
    if unity_hits >= 3:
        engine = Engine.UNITY
    elif scores.get(Engine.UNREAL):
        engine = Engine.UNREAL
    elif scores.get(Engine.GODOT):
        engine = Engine.GODOT
    elif scores.get(Engine.LIBGDX):
        engine = Engine.LIBGDX
    elif scores.get(Engine.COCOS):
        engine = Engine.COCOS
    elif scores.get(Engine.DEFOLD):
        engine = Engine.DEFOLD
    elif scores.get(Engine.CORONA):
        engine = Engine.CORONA
    elif native:
        engine = Engine.NATIVE
    else:
        engine = Engine.UNKNOWN

    unity_version = _probe_unity_version(root, rel)

    report = {
        "stage": "engine_detect",
        "engine": engine,
        "unity_hits": unity_hits,
        "scores": scores,
        "evidence": {k: v[:60] for k, v in evidence.items()},
        "dex_count": len(dex),
        "has_resources_arsc": arsc,
        "native_libs": sorted(set(sos))[:60],
        "abis": sorted({k.split("/")[1] for k in rel if k.startswith("lib/") and k.count("/") >= 2}),
        "file_count": len(files),
        "unity_version": unity_version,
        "spine_signals": detect_spine_signals(root, rel),
    }
    log(f"engine={engine} unity_hits={unity_hits} unity_version={unity_version}")
    return report


def _probe_unity_version(root: Path, rel: Dict[str, Path]) -> Optional[str]:
    """Read the engine version out of globalgamemanagers / data.unity3d headers."""
    pat = re.compile(rb"(20\d{2}|5|4)\.\d+\.\d+[a-z]?\d*")
    for key in ("assets/bin/Data/globalgamemanagers", "assets/bin/Data/data.unity3d"):
        p = rel.get(key)
        if not p:
            continue
        try:
            blob = p.read_bytes()[:4096]
        except Exception:  # noqa: BLE001
            continue
        m = pat.search(blob)
        if m:
            return m.group(0).decode()
    # raw SerializedFile: the version string sits right after the v22 header
    for k, p in rel.items():
        if not (k.endswith(".assets") or k.endswith(".resource") or k.endswith(".resS")):
            continue
        try:
            blob = p.read_bytes()[:128]
        except Exception:  # noqa: BLE001
            continue
        m = pat.search(blob)
        if m:
            return m.group(0).decode()
    # UnityFS bundles embed "5.x.x\0<engine version>\0"
    for k, p in rel.items():
        if _head(p, 8).startswith(UNITYFS_MAGIC):
            try:
                blob = p.read_bytes()[:256]
            except Exception:  # noqa: BLE01
                continue
            parts = blob.split(b"\x00")
            for part in parts[3:6]:
                if pat.fullmatch(part):
                    return part.decode()
    return None


def detect_spine_signals(root: Path, rel: Optional[Dict[str, Path]] = None) -> Dict[str, Any]:
    """Cheap pre-scan: does this build look like it carries Spine content?"""
    rel = rel or {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}
    sig: Dict[str, Any] = {"atlas_files": [], "json_candidates": 0, "skel_candidates": [],
                           "so_runtime": [], "dll_runtime": []}
    for k in rel:
        lk = k.lower()
        if lk.endswith(".atlas") or lk.endswith(".atlas.txt") or ".atlas" in lk:
            sig["atlas_files"].append(k)
        if "spine" in lk:
            if lk.endswith(".so"):
                sig["so_runtime"].append(k)
            if lk.endswith(".dll"):
                sig["dll_runtime"].append(k)
    sig["atlas_files"] = sig["atlas_files"][:50]
    sig["likely"] = bool(sig["atlas_files"] or sig["so_runtime"] or sig["dll_runtime"])
    return sig


def save(report: Dict[str, Any], out: Path) -> Dict[str, Any]:
    write_json(out, report)
    return report
