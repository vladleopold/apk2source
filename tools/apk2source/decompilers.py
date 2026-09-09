"""External decompiler wrappers with self-provisioning.

Each tool is fetched from its official GitHub release on first use and cached
under `~/.cache/apk2source/tools` (or `$APK2SOURCE_TOOLS`), so a fresh CI runner
needs nothing but network access.

    apktool       resources.arsc / smali / AndroidManifest.xml -> readable xml
    jadx          classes*.dex -> Java sources
    AssetRipper   Unity binaries -> a reconstructable Unity project (C# + assets)
    Il2CppDumper  libil2cpp.so + global-metadata.dat -> C# signatures / DummyDll
    utinyripper   (optional legacy fallback for AssetRipper)

Every wrapper is best-effort: a tool that cannot run is reported, never fatal.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .util import ensure_dir, human, log, run, warn, which, write_json

TOOLS_ROOT = Path(os.environ.get("APK2SOURCE_TOOLS", str(Path.home() / ".cache/apk2source/tools")))

RELEASES = {
    "apktool": {
        "url": "https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar",
        "kind": "jar",
        "needs": ["java"],
    },
    "jadx": {
        "url": "https://github.com/skylot/jadx/releases/download/v1.5.0/jadx-1.5.0.zip",
        "kind": "zip",
        "entry": "bin/jadx",
        "needs": ["java"],
    },
    "assetripper": {
        "url": "https://github.com/AssetRipper/AssetRipper/releases/download/1.1.0/AssetRipper_linux_x64.zip",
        "kind": "zip",
        "entry": "AssetRipper",
        "platform_map": {"Darwin": "AssetRipper_macos_x64.zip", "Linux": "AssetRipper_linux_x64.zip",
                         "Windows": "AssetRipper_win_x64.zip"},
    },
    "il2cppdumper": {
        "url": "https://github.com/Perfare/Il2CppDumper/releases/download/v6.7.40/Il2CppDumper-linux-x64.zip",
        "kind": "zip",
        "entry": "Il2CppDumper",
        "platform_map": {"Darwin": "Il2CppDumper-osx-x64.zip", "Linux": "Il2CppDumper-linux-x64.zip",
                         "Windows": "Il2CppDumper-win-x64.zip"},
    },
}


def _download(url: str, dest: Path, timeout: int = 1800) -> Path:
    ensure_dir(dest.parent)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"downloading {url}")
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "apk2source"})
            with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:  # noqa: S310
                shutil.copyfileobj(r, f, 1 << 20)
            tmp.replace(dest)
            return dest
        except Exception as e:  # noqa: BLE001
            warn(f"download attempt {attempt} failed: {e}")
            time.sleep(5 * attempt)
    raise RuntimeError(f"could not download {url}")


def provision(name: str, *, force: bool = False) -> Optional[Path]:
    """Make sure a tool is available; return its entrypoint path."""
    name = name.lower()
    spec = RELEASES.get(name)
    if spec is None:
        warn(f"unknown tool: {name}")
        return None

    existing = which(name) or which(name.replace("assetripper", "AssetRipper"))
    if existing and not force:
        return Path(existing)

    root = ensure_dir(TOOLS_ROOT / name)
    url = spec["url"]
    pmap = spec.get("platform_map")
    if pmap:
        url = url.rsplit("/", 1)[0] + "/" + pmap.get(platform.system(), url.rsplit("/", 1)[-1])

    entry = root / (spec.get("entry") or Path(url).name)
    if entry.exists() and os.access(entry, os.X_OK | os.R_OK):
        return entry
    if spec["kind"] == "jar":
        jar = root / Path(url).name
        if not jar.exists():
            _download(url, jar)
        return jar

    archive = root / Path(url).name
    if not archive.exists():
        _download(url, archive)
    if spec["kind"] == "zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(root)
    elif spec["kind"] == "tar":
        with tarfile.open(archive) as t:
            t.extractall(root)
    for p in root.rglob("*"):
        if p.is_file() and (p.suffix in ("", ".sh") or p.name == spec.get("entry")):
            try:
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass
    return entry if entry.exists() else None


# --------------------------------------------------------------------------
# wrappers
# --------------------------------------------------------------------------
def apktool(apk: Path, out: Path, *, decode_sources: bool = False) -> Dict[str, Any]:
    jar = provision("apktool")
    rec: Dict[str, Any] = {"tool": "apktool", "ok": False}
    if not jar or not which("java"):
        rec["error"] = "apktool/java unavailable"
        return rec
    ensure_dir(out)
    args = ["java", "-jar", str(jar), "d", "-f", "-o", str(out), str(apk)]
    if not decode_sources:
        args.insert(4, "-s")
    p = run(args, check=False, timeout=3600)
    rec["ok"] = p.returncode == 0
    rec["stdout"] = (p.stdout or "")[-3000:]
    rec["stderr"] = (p.stderr or "")[-3000:]
    rec["out"] = str(out)
    return rec


def jadx(apk: Path, out: Path, *, threads: int = 4) -> Dict[str, Any]:
    entry = provision("jadx")
    rec: Dict[str, Any] = {"tool": "jadx", "ok": False}
    if not entry:
        rec["error"] = "jadx unavailable"
        return rec
    ensure_dir(out)
    cmd = [str(entry), "-d", str(out), "-j", str(threads), "--no-res", "--show-bad-code", str(apk)]
    if entry.suffix == ".jar":
        cmd = ["java", "-jar", str(entry)] + cmd[1:]
    p = run(cmd, check=False, timeout=7200)
    rec["ok"] = p.returncode in (0, 1)  # jadx exits 1 on partial decompilation
    rec["stdout"] = (p.stdout or "")[-3000:]
    rec["stderr"] = (p.stderr or "")[-3000:]
    rec["out"] = str(out)
    rec["java_files"] = sum(1 for _ in Path(out).rglob("*.java")) if Path(out).exists() else 0
    return rec


def assetripper(assets_dir: Path, out: Path, *, timeout: int = 7200) -> Dict[str, Any]:
    """Reconstruct a Unity project (ExportedProject) from extracted Unity data."""
    entry = provision("assetripper")
    rec: Dict[str, Any] = {"tool": "assetripper", "ok": False}
    if not entry or not entry.exists():
        rec["error"] = "AssetRipper unavailable"
        rec["hint"] = "set APK2SOURCE_ASSETRIPPER to a local AssetRipper binary"
        return rec
    ensure_dir(out)
    cmd = [str(entry), assets_dir, "-o", str(out), "--export", "StandardContent"]
    p = run(cmd, check=False, timeout=timeout)
    rec["ok"] = p.returncode == 0
    rec["stdout"] = (p.stdout or "")[-4000:]
    rec["stderr"] = (p.stderr or "")[-4000:]
    rec["out"] = str(out)
    if Path(out).exists():
        rec["cs_files"] = sum(1 for _ in Path(out).rglob("*.cs"))
        rec["prefabs"] = sum(1 for _ in Path(out).rglob("*.prefab"))
        rec["materials"] = sum(1 for _ in Path(out).rglob("*.mat"))
    return rec


def il2cppdumper(merged_dir: Path, out: Path) -> Dict[str, Any]:
    """libil2cpp.so + global-metadata.dat -> C# signatures."""
    entry = provision("il2cppdumper")
    rec: Dict[str, Any] = {"tool": "il2cppdumper", "ok": False}
    if not entry or not entry.exists():
        rec["error"] = "Il2CppDumper unavailable"
        return rec
    so = next(iter(sorted(merged_dir.rglob("libil2cpp.so"))), None)
    meta = next(iter(sorted(merged_dir.rglob("global-metadata.dat"))), None)
    if not so or not meta:
        rec["error"] = "libil2cpp.so or global-metadata.dat not found (not an IL2CPP build?)"
        return rec
    ensure_dir(out)
    p = run([str(entry), str(so), str(meta), str(out)], check=False, timeout=3600)
    rec["ok"] = p.returncode == 0
    rec["libil2cpp"] = str(so)
    rec["metadata"] = str(meta)
    rec["out"] = str(out)
    rec["stdout"] = (p.stdout or "")[-3000:]
    return rec


def survey(merged_dir: Path) -> Dict[str, Any]:
    """What decompilation targets does this build actually offer?"""
    merged_dir = Path(merged_dir)
    out: Dict[str, Any] = {"dex": [], "native": [], "unity": [], "metadata": []}
    for p in merged_dir.rglob("*.dex"):
        out["dex"].append({"path": p.relative_to(merged_dir).as_posix(), "bytes": p.stat().st_size})
    for p in merged_dir.rglob("*.so"):
        out["native"].append({"path": p.relative_to(merged_dir).as_posix(), "bytes": p.stat().st_size})
    for p in merged_dir.rglob("global-metadata.dat"):
        out["metadata"].append({"path": p.relative_to(merged_dir).as_posix(), "bytes": p.stat().st_size})
    for pat in ("assets/bin/Data/**", "assets/aa/**", "**/*.bundle"):
        for p in merged_dir.glob(pat):
            if p.is_file():
                out["unity"].append({"path": p.relative_to(merged_dir).as_posix(),
                                     "bytes": p.stat().st_size})
    out["can_jadx"] = bool(out["dex"])
    out["can_il2cpp"] = bool(out["metadata"]) and any("libil2cpp" in n["path"] for n in out["native"])
    out["can_assetripper"] = bool(out["unity"])
    return out
