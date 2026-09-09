"""Stage 03 - DEVICE CACHE CAPTURE.

Runs the app on an Android device/emulator, lets it perform its first-run
download, then separates "what the app fetched at runtime" from "what shipped
inside the APK".

Method (snapshot diff, no root required):
  1. snapshot A  - the app's external+internal data dirs *before* first launch
  2. install      - `adb install-multiple` for split sets, `adb install` otherwise
  3. snapshot B  - right after install, before launch
  4. launch + wait for the first-run download to settle (network idle + size stable)
  5. snapshot C  - after first run
  6. cache = C - B   (files that appeared/grew because the app ran)
     shipped = B - A (files the installer wrote)

Everything under `cache/` is what the next stage unpacks.

Designed to run inside `reactivecircus/android-emulator-runner` on a
`macos-latest` runner (hardware acceleration) or a KVM-enabled Linux runner.
Every adb call is best-effort: a missing device degrades to "no cache" rather
than failing the pipeline.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .util import ensure_dir, human, log, run, sha256_file, warn, which, write_json

ADB = "adb"


# --------------------------------------------------------------------------
# adb plumbing
# --------------------------------------------------------------------------
def adb(*args: str, serial: Optional[str] = None, timeout: int = 300,
        check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    cmd: List[str] = [ADB]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    return run(cmd, timeout=timeout, check=check, capture=capture)


def adb_out(*args: str, serial: Optional[str] = None, timeout: int = 300) -> str:
    p = adb(*args, serial=serial, timeout=timeout)
    return (p.stdout or "").strip()


def find_device(serial: Optional[str] = None, wait: int = 300) -> Optional[str]:
    if not which(ADB):
        warn("adb not found on PATH")
        return None
    deadline = time.time() + wait
    while time.time() < deadline:
        out = adb_out("devices")
        devices = [ln.split("\t")[0] for ln in out.splitlines()[1:] if "\tdevice" in ln]
        if devices:
            return serial if serial in devices else devices[0]
        time.sleep(5)
    warn("no adb device became available")
    return None


def device_info(serial: str) -> Dict[str, Any]:
    def prop(name: str) -> str:
        return adb_out("shell", "getprop", name, serial=serial)

    info = {
        "serial": serial,
        "model": prop("ro.product.model"),
        "manufacturer": prop("ro.product.manufacturer"),
        "android_version": prop("ro.build.version.release"),
        "sdk": prop("ro.build.version.sdk"),
        "abi": prop("ro.product.cpu.abi"),
        "abis": prop("ro.product.cpu.abilist"),
        "density": prop("ro.sf.lcd_density"),
        "opengl": prop("ro.opengles.version"),
    }
    try:
        mem = adb_out("shell", "cat", "/proc/meminfo", serial=serial)
        m = re.search(r"MemTotal:\s+(\d+)\s*kB", mem)
        info["ram_mb"] = int(m.group(1)) // 1024 if m else None
    except Exception:  # noqa: BLE001
        pass
    return info


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
def install(splits_dir: Path, package: Optional[str], serial: str,
            *, grant_permissions: bool = True) -> Dict[str, Any]:
    """Install a split set (install-multiple) or a single apk."""
    splits_dir = Path(splits_dir)
    apks = sorted(p for p in splits_dir.rglob("*.apk"))
    rec: Dict[str, Any] = {"apk_count": len(apks), "mode": None, "ok": False}
    if not apks:
        rec["error"] = "no apk found"
        return rec

    base = next((p for p in apks if p.name.lower().startswith("base")), apks[0])
    others = [p for p in apks if p != base]

    # ABI filtering: only push splits matching the device, else install fails
    abi = device_info(serial).get("abi") or ""
    keep: List[Path] = [base]
    for p in others:
        n = p.name.lower()
        m = re.search(r"config\.(arm64-v8a|armeabi-v7a|x86_64|x86|universal)\.apk", n)
        if m and m.group(1) not in ("universal",) and abi and m.group(1) != abi:
            log(f"  skipping abi split {p.name} (device abi={abi})")
            continue
        keep.append(p)
    rec["installed"] = [p.name for p in keep]

    if len(keep) == 1:
        rec["mode"] = "install"
        p = adb("install", "-r", "-g" if grant_permissions else "-r", str(keep[0]),
                serial=serial, timeout=1800)
    else:
        rec["mode"] = "install-multiple"
        p = adb("install-multiple", "-r", "-g" if grant_permissions else "-r",
                *[str(k) for k in keep], serial=serial, timeout=3600)
    rec["stdout"] = (p.stdout or "")[-4000:]
    rec["stderr"] = (p.stderr or "")[-4000:]
    rec["returncode"] = p.returncode
    rec["ok"] = p.returncode == 0 and "Success" in ((p.stdout or "") + (p.stderr or ""))
    log(f"install ({rec['mode']}) -> ok={rec['ok']}")
    return rec


def resolve_package(serial: str, hint: Optional[str] = None) -> Optional[str]:
    if hint:
        return hint
    out = adb_out("shell", "pm", "list", "packages", "-3", serial=serial)
    pkgs = [ln.replace("package:", "").strip() for ln in out.splitlines() if ln.startswith("package:")]
    return pkgs[-1] if pkgs else None


def launch(serial: str, package: str) -> Dict[str, Any]:
    rec: Dict[str, Any] = {"package": package}
    out = adb_out("shell", "cmd", "package", "resolve-activity", "--brief", package, serial=serial)
    comp = out.splitlines()[-1] if out else ""
    if "/" not in comp:
        # fall back to monkey, which resolves the launcher intent for us
        p = adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1",
                serial=serial, timeout=120)
        rec["method"] = "monkey"
        rec["ok"] = p.returncode == 0
    else:
        p = adb("shell", "am", "start", "-n", comp, serial=serial, timeout=120)
        rec["method"] = "am start"
        rec["component"] = comp
        rec["ok"] = p.returncode == 0
    rec["stdout"] = (p.stdout or "")[-2000:]
    log(f"launch {package} via {rec.get('method')} -> ok={rec.get('ok')}")
    return rec


# --------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------
SCAN_ROOTS = (
    "/sdcard/Android/data/{pkg}",
    "/sdcard/Android/obb/{pkg}",
    "/sdcard/{pkg}",
    "/data/data/{pkg}",
    "/data/user/0/{pkg}",
)


@dataclass
class Snapshot:
    label: str
    files: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(v.get("size", 0) for v in self.files.values())

    def to_json(self) -> Dict[str, Any]:
        return {"label": self.label, "count": len(self.files), "total": self.total,
                "files": self.files}


def _ls_recursive(serial: str, root: str) -> Dict[str, Dict[str, Any]]:
    """`ls -lR` over adb; returns {path: {size, mtime, kind}}."""
    out = adb_out("shell", "ls", "-lRa", root, serial=serial, timeout=300)
    files: Dict[str, Dict[str, Any]] = {}
    cwd = root
    for ln in out.splitlines():
        ln = ln.rstrip()
        if not ln:
            continue
        if ln.endswith(":"):
            cwd = ln[:-1]
            continue
        if ln.startswith("total"):
            continue
        parts = ln.split()
        if len(parts) < 7:
            continue
        perms, name = parts[0], parts[-1]
        if name in (".", ".."):
            continue
        try:
            size = int(parts[3]) if parts[3].isdigit() else 0
        except Exception:  # noqa: BLE001
            size = 0
        path = f"{cwd}/{name}" if not name.startswith("/") else name
        files[path] = {
            "size": size,
            "mtime": " ".join(parts[4:6]),
            "kind": "dir" if perms.startswith("d") else ("link" if perms.startswith("l") else "file"),
        }
    return files


def snapshot(serial: str, package: str, label: str) -> Snapshot:
    snap = Snapshot(label=label)
    for tmpl in SCAN_ROOTS:
        root = tmpl.format(pkg=package)
        exists = adb_out("shell", "[", "-d", root, "]", "&&", "echo", "yes", serial=serial)
        if "yes" not in exists:
            continue
        try:
            snap.files.update(_ls_recursive(serial, root))
        except Exception as e:  # noqa: BLE001
            warn(f"snapshot {root} failed: {e}")
    snap.files = {k: v for k, v in snap.files.items() if v["kind"] == "file"}
    log(f"snapshot[{label}] {len(snap.files)} files, {human(snap.total)}")
    return snap


def diff(before: Snapshot, after: Snapshot) -> Dict[str, Dict[str, Any]]:
    """Files that appeared or grew between two snapshots."""
    out: Dict[str, Dict[str, Any]] = {}
    for path, meta in after.files.items():
        prev = before.files.get(path)
        if prev is None:
            out[path] = {**meta, "change": "new"}
        elif meta["size"] != prev["size"]:
            out[path] = {**meta, "change": "grown", "previous_size": prev["size"]}
    return out


# --------------------------------------------------------------------------
# settling: wait until the first-run download stops growing
# --------------------------------------------------------------------------
def wait_for_idle(serial: str, package: str, *, quiet_rounds: int = 3,
                  interval: int = 15, max_wait: int = 900) -> Dict[str, Any]:
    """Poll the data dirs until total size is stable for `quiet_rounds` polls."""
    history: List[Dict[str, Any]] = []
    last_total = -1
    stable = 0
    started = time.time()
    while time.time() - started < max_wait:
        snap = snapshot(serial, package, "poll")
        total = snap.total
        history.append({"t": round(time.time() - started, 1), "files": len(snap.files), "bytes": total})
        log(f"  settle: {len(snap.files)} files / {human(total)} (stable={stable})")
        if total == last_total:
            stable += 1
            if stable >= quiet_rounds:
                break
        else:
            stable = 0
        last_total = total
        time.sleep(interval)
    return {"settled": stable >= quiet_rounds, "seconds": round(time.time() - started, 1),
            "history": history}


# --------------------------------------------------------------------------
# pull
# --------------------------------------------------------------------------
def pull_paths(serial: str, paths: Sequence[str], dest: Path, *, max_bytes: int = 0) -> Dict[str, Any]:
    dest = ensure_dir(dest)
    pulled, failed, total = 0, 0, 0
    for remote in paths:
        local = dest / re.sub(r"^/+", "", remote).replace("/", os.sep)
        ensure_dir(local.parent)
        p = adb("pull", remote, str(local), serial=serial, timeout=1800)
        if p.returncode == 0 and local.exists():
            pulled += 1
            total += local.stat().st_size
        else:
            failed += 1
    log(f"pulled {pulled} files ({human(total)}), {failed} failed")
    return {"pulled": pulled, "failed": failed, "bytes": total, "dest": str(dest)}


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
def capture(splits_dir: Path, out_dir: Path, *, package: Optional[str] = None,
            serial: Optional[str] = None, settle_seconds: int = 900,
            pull_cache: bool = True, skip_install: bool = False) -> Dict[str, Any]:
    """Full device-cache capture. Returns a stage record."""
    out_dir = ensure_dir(out_dir)
    meta_dir = ensure_dir(out_dir / "meta")
    rec: Dict[str, Any] = {"stage": "device_cache", "out": str(out_dir), "ok": False,
                           "cache_files": 0, "cache_bytes": 0}

    serial = serial or find_device()
    if not serial:
        rec["error"] = "no device"
        rec["degraded"] = True
        write_json(meta_dir / "device_cache.json", rec)
        warn("device cache stage skipped: no adb device")
        return rec

    rec["device"] = device_info(serial)
    write_json(meta_dir / "device.json", rec["device"])

    if not skip_install:
        rec["install"] = install(Path(splits_dir), package, serial)
        if not rec["install"]["ok"]:
            rec["error"] = "install failed"
            write_json(meta_dir / "device_cache.json", rec)
            return rec

    pkg = resolve_package(serial, package)
    if not pkg:
        rec["error"] = "package not resolved"
        write_json(meta_dir / "device_cache.json", rec)
        return rec
    rec["package"] = pkg

    snap_pre = snapshot(serial, pkg, "pre_install")
    if not skip_install:
        # reinstall is not needed; snapshot right after install instead
        pass
    snap_post_install = snapshot(serial, pkg, "post_install")

    rec["launch"] = launch(serial, pkg)
    rec["settle"] = wait_for_idle(serial, pkg, max_wait=settle_seconds)
    snap_post_run = snapshot(serial, pkg, "post_run")

    cache = diff(snap_post_install, snap_post_run)
    shipped = diff(snap_pre, snap_post_install)
    rec["cache_files"] = len(cache)
    rec["cache_bytes"] = sum(v["size"] for v in cache.values())
    rec["shipped_files"] = len(shipped)
    rec["shipped_bytes"] = sum(v["size"] for v in shipped.values())

    write_json(meta_dir / "snapshot_pre_install.json", snap_pre.to_json())
    write_json(meta_dir / "snapshot_post_install.json", snap_post_install.to_json())
    write_json(meta_dir / "snapshot_post_run.json", snap_post_run.to_json())
    write_json(meta_dir / "cache_manifest.json", cache)
    write_json(meta_dir / "shipped_manifest.json", shipped)

    if pull_cache and cache:
        rec["pull"] = pull_paths(serial, sorted(cache.keys()), out_dir / "cache")
        rec["cache_dir"] = str(out_dir / "cache")
    rec["ok"] = True
    write_json(meta_dir / "device_cache.json", rec)
    log(f"device cache: {rec['cache_files']} new/grown files, {human(rec['cache_bytes'])}")
    return rec
