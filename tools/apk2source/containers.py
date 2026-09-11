"""Stage 02 - UNPACK.

Normalises every Android distribution container into a single "merged APK view":

  .apks   SAI / bundletool split set  (base.apk + split_config.*.apk + split_<pack>.apk)
  .xapk   APKPure bundle              (manifest.json + *.apk + optional OBB)
  .apkm   APKMirror bundle            (info.json + *.apk)
  .aab    Android App Bundle          (base/ + <asset pack>/ + BundleConfig.pb)
  .apk    plain APK                   (already merged)

Output layout (out_dir):
    merged/            the reconstructed single-APK tree (base + splits overlaid)
    splits/            the individual split payloads, untouched
    meta/              container manifest, package info, asset-pack map
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .util import ensure_dir, human, log, read_json, safe_join, sha256_file, warn, write_json

CONTAINER_KINDS = ("apks", "xapk", "apkm", "aab", "apk", "zip")


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------
def detect_kind(path: Path) -> str:
    suf = path.suffix.lower().lstrip(".")
    if suf in ("apks", "xapk", "apkm", "aab"):
        return suf
    if suf == "apk":
        return "apk"
    if not zipfile.is_zipfile(path):
        return "unknown"
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "base.apk" in names or any(n.startswith("split_") for n in names):
            return "apks"
        if any(n == "manifest.json" for n in names) and any(n.endswith(".apk") for n in names):
            return "xapk"
        if "BundleConfig.pb" in names or "base/manifest/AndroidManifest.xml" in names:
            return "aab"
        if "AndroidManifest.xml" in names:
            return "apk"
    return "zip"


def is_split_apk_set(path: Path) -> bool:
    return detect_kind(path) in ("apks", "xapk", "apkm")


# --------------------------------------------------------------------------
# zip helpers
# --------------------------------------------------------------------------
def _extract(zf: zipfile.ZipFile, member: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out, 1 << 20)
    return dest


def _zip_list(path: Path) -> List[Dict[str, Any]]:
    with zipfile.ZipFile(path) as z:
        return [
            {
                "name": i.filename,
                "size": i.file_size,
                "compressed": i.compress_size,
                "crc": f"{i.CRC:08x}",
                "dir": i.is_dir(),
            }
            for i in z.infolist()
        ]


# --------------------------------------------------------------------------
# AndroidManifest (binary AXML) minimal parser -> package / label / sdk
# --------------------------------------------------------------------------
_AXML_STR_POOL = 0x0001
_AXML_RES_MAP = 0x0180
_AXML_START_NS = 0x0100
_AXML_START_TAG = 0x0102
_ATTR_PACKAGE = 0x00000000  # not a resource id; handled by name


def parse_axml_strings(data: bytes) -> List[str]:
    """Extract the string pool from a binary AndroidManifest.xml."""
    if len(data) < 8 or data[0:2] != b"\x03\x00":
        return []
    import struct

    pos = 8
    strings: List[str] = []
    while pos + 8 <= len(data):
        ctype, hsize, size = struct.unpack_from("<HHI", data, pos)
        if size == 0 or pos + size > len(data):
            break
        if ctype == _AXML_STR_POOL:
            (string_count, _style_count, flags, strings_start, _styles_start) = struct.unpack_from(
                "<IIIII", data, pos + 8
            )
            offs = [struct.unpack_from("<I", data, pos + 28 + 4 * i)[0] for i in range(string_count)]
            utf8 = bool(flags & (1 << 8))
            base = pos + strings_start
            for o in offs:
                p = base + o
                try:
                    if utf8:
                        # u16-len (chars), u16-len (bytes) - both may be 1 or 2 bytes
                        n = data[p]
                        p += 2 if n & 0x80 else 1
                        bl = data[p]
                        p += 2 if bl & 0x80 else 1
                        strings.append(data[p:p + bl].decode("utf-8", "replace"))
                    else:
                        n = struct.unpack_from("<H", data, p)[0]
                        if n & 0x8000:
                            n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p + 2)[0]
                            p += 4
                        else:
                            p += 2
                        strings.append(data[p:p + n * 2].decode("utf-16-le", "replace"))
                except Exception:  # noqa: BLE001
                    strings.append("")
            break
        pos += size
    return strings


def _manifest_package_from_strings(strings: List[str]) -> Optional[str]:
    pkg = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z0-9_]+){1,}$")
    cands = [s for s in strings if pkg.match(s) and len(s) < 200]
    ranked = sorted(cands, key=lambda s: (not s.count(".") >= 2, len(s)))
    return ranked[0] if ranked else None


def _root_manifest_package(data: bytes, strings: List[str]) -> Optional[str]:
    pos = 8
    while pos + 8 <= len(data):
        ctype, _hsize, size = struct.unpack_from("<HHI", data, pos)
        if size == 0 or pos + size > len(data):
            break
        if ctype == _AXML_START_TAG and size >= 36:
            name_idx = struct.unpack_from("<I", data, pos + 20)[0]
            attr_start = struct.unpack_from("<H", data, pos + 24)[0]
            attr_size = struct.unpack_from("<H", data, pos + 26)[0]
            attr_count = struct.unpack_from("<H", data, pos + 28)[0]
            if name_idx < len(strings) and strings[name_idx] == "manifest" and attr_size >= 20:
                attrs_start = pos + 16 + attr_start
                attrs_end = attrs_start + attr_count * attr_size
                if attrs_end <= pos + size:
                    for off in range(attrs_start, attrs_end, attr_size):
                        ns_idx = struct.unpack_from("<I", data, off)[0]
                        name_idx = struct.unpack_from("<I", data, off + 4)[0]
                        raw_value = struct.unpack_from("<I", data, off + 8)[0]
                        value_size, _res0, value_type, value_data = struct.unpack_from("<HBBI", data, off + 12)
                        if ns_idx == 0xFFFFFFFF and name_idx < len(strings) and strings[name_idx] == "package":
                            candidates = [raw_value]
                            if value_size >= 8 and value_type == 0x03:
                                candidates.append(value_data)
                            for idx in candidates:
                                if idx < len(strings) and strings[idx]:
                                    return strings[idx]
        pos += size
    return None


def _manifest_package_from_data(data: bytes) -> Optional[str]:
    strings = parse_axml_strings(data)
    return _root_manifest_package(data, strings) or _manifest_package_from_strings(strings)


def parse_manifest(path: Path) -> Dict[str, Any]:
    """Best-effort package/version extraction from an APK's binary manifest."""
    out: Dict[str, Any] = {}
    try:
        with zipfile.ZipFile(path) as z:
            data = z.read("AndroidManifest.xml")
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out

    strings = parse_axml_strings(data)
    out["string_pool_size"] = len(strings)
    pkg = _manifest_package_from_data(data)
    if pkg:
        out["package"] = pkg
    for key in ("versionName", "versionCode", "minSdkVersion", "targetSdkVersion"):
        if key in strings:
            out.setdefault("attr_names_seen", []).append(key)
    return out


# --------------------------------------------------------------------------
# main unpack
# --------------------------------------------------------------------------
def unpack(payload: Path, out_dir: Path, *, keep_splits: bool = True) -> Dict[str, Any]:
    payload = Path(payload)
    out_dir = ensure_dir(out_dir)
    kind = detect_kind(payload)
    log(f"container kind: {kind} ({human(payload.stat().st_size)})")

    splits_dir = ensure_dir(out_dir / "splits")
    merged_dir = ensure_dir(out_dir / "merged")
    meta_dir = ensure_dir(out_dir / "meta")

    record: Dict[str, Any] = {
        "stage": "unpack",
        "input": str(payload),
        "kind": kind,
        "input_sha256": sha256_file(payload),
        "input_size": payload.stat().st_size,
        "splits": [],
        "asset_packs": [],
        "merged_entries": 0,
        "merged": str(merged_dir),
        "splits_dir": str(splits_dir),
    }

    if kind == "apk":
        _explode_apk(payload, merged_dir)
        record["splits"] = [{"name": payload.name, "role": "base", "path": str(payload)}]
        record["package"] = parse_manifest(payload).get("package")
    elif kind in ("apks", "xapk", "apkm"):
        _unpack_split_set(payload, kind, splits_dir, merged_dir, record, keep_splits)
    elif kind == "aab":
        _unpack_aab(payload, splits_dir, merged_dir, record)
    else:
        _explode_apk(payload, merged_dir)

    record["merged_entries"] = sum(1 for p in merged_dir.rglob("*") if p.is_file())
    record["merged_size"] = sum(p.stat().st_size for p in merged_dir.rglob("*") if p.is_file())
    record["package"] = record.get("package") or _guess_package(merged_dir)
    write_json(meta_dir / "unpack.json", record)
    write_json(meta_dir / "merged_listing.json", _summarise_tree(merged_dir))
    log(f"unpacked -> {record['merged_entries']} files, {human(record.get('merged_size',0))}, "
        f"package={record.get('package')}")
    return record


def _explode_apk(apk: Path, dest: Path) -> None:
    with zipfile.ZipFile(apk) as z:
        for i in z.infolist():
            if i.is_dir():
                continue
            _extract(z, i.filename, safe_join(dest, i.filename))


def _unpack_split_set(payload: Path, kind: str, splits_dir: Path, merged_dir: Path,
                      record: Dict[str, Any], keep_splits: bool) -> None:
    with zipfile.ZipFile(payload) as z:
        members = [i for i in z.infolist() if not i.is_dir()]
        apks = [i for i in members if i.filename.lower().endswith(".apk")]
        metas = [i for i in members if i.filename.lower().endswith(".json")]

        for m in metas:
            try:
                record.setdefault("container_meta", {})[m.filename] = json.loads(z.read(m.filename))
            except Exception:  # noqa: BLE001
                pass

        if not apks:
            warn(f"{kind} contains no .apk members - falling back to raw explode")
            for i in members:
                _extract(z, i.filename, safe_join(merged_dir, i.filename))
            return

        base: Optional[zipfile.ZipInfo] = None
        configs: List[zipfile.ZipInfo] = []
        packs: List[zipfile.ZipInfo] = []
        for i in sorted(apks, key=lambda x: x.filename):
            n = i.filename.lower()
            if n in ("base.apk", "base-master.apk") or n.endswith("base-master.apk"):
                base = base or i
            elif "split_config" in n or re.search(r"config\.(xx?hdpi|mdpi|hdpi|xhdpi|xxxhdpi|nodpi|anydpi)", n):
                configs.append(i)
            elif re.search(r"(armeabi|arm64|v7a|x86|x86_64)", n) and "config" in n:
                configs.append(i)
            else:
                packs.append(i)
        if base is None:
            base = apks[0]

        # extract each split
        extracted: Dict[str, Path] = {}
        for i in [base] + configs + packs:
            role = "base" if i is base else ("config" if i in configs else "asset_pack")
            dest = splits_dir / i.filename
            _extract(z, i.filename, dest)
            extracted[i.filename] = dest
            entry = {
                "name": i.filename,
                "role": role,
                "size": i.file_size,
                "path": str(dest),
                "sha256": sha256_file(dest),
            }
            record["splits"].append(entry)
            if role == "asset_pack":
                record["asset_packs"].append(entry)
            if role == "base":
                record["package"] = parse_manifest(dest).get("package")

        # merge: base first, then configs, then asset packs (later wins)
        order = [base] + configs + packs
        for i in order:
            apk = extracted[i.filename]
            try:
                with zipfile.ZipFile(apk) as az:
                    for m in az.infolist():
                        if m.is_dir():
                            continue
                        # skip per-split signature/meta files that would collide
                        if m.filename.startswith("META-INF/") and m.filename.upper().endswith(
                            (".SF", ".RSA", ".DSA", ".EC", ".MF")
                        ):
                            continue
                        _extract(az, m.filename, safe_join(merged_dir, m.filename))
            except zipfile.BadZipFile:
                warn(f"split {i.filename} is not a readable zip, skipped in merge")

        if not keep_splits:
            shutil.rmtree(splits_dir, ignore_errors=True)
            record["splits_dir"] = None


def _unpack_aab(payload: Path, splits_dir: Path, merged_dir: Path, record: Dict[str, Any]) -> None:
    """An .aab is already a directory tree inside a zip: base/, <pack>/, BundleConfig.pb."""
    with zipfile.ZipFile(payload) as z:
        for i in z.infolist():
            if i.is_dir():
                continue
            _extract(z, i.filename, safe_join(merged_dir, i.filename))
            top = i.filename.split("/")[0]
            if top not in ("base", "BundleConfig.pb", "BUNDLE-METADATA"):
                record["asset_packs"].append({"name": top, "role": "asset_pack", "entry": i.filename})
    seen = {p["name"] for p in record["asset_packs"]}
    record["asset_packs"] = [{"name": n, "role": "asset_pack"} for n in sorted(seen)]


def _guess_package(merged_dir: Path) -> Optional[str]:
    mf = merged_dir / "AndroidManifest.xml"
    if mf.exists():
        return parse_manifest(mf.parent if mf.parent.name else mf) if False else _pkg_from_manifest_file(mf)
    return None


def _pkg_from_manifest_file(mf: Path) -> Optional[str]:
    try:
        data = mf.read_bytes()
    except Exception:  # noqa: BLE001
        return None
    return _manifest_package_from_data(data)


def _summarise_tree(root: Path, top_n: int = 40) -> Dict[str, Any]:
    by_top: Dict[str, Dict[str, int]] = {}
    exts: Dict[str, int] = {}
    total = 0
    largest: List[Tuple[int, str]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        top = rel.split("/")[0]
        sz = p.stat().st_size
        total += sz
        d = by_top.setdefault(top, {"files": 0, "bytes": 0})
        d["files"] += 1
        d["bytes"] += sz
        e = p.suffix.lower() or "(none)"
        exts[e] = exts.get(e, 0) + 1
        largest.append((sz, rel))
    largest.sort(reverse=True)
    return {
        "total_bytes": total,
        "total_human": human(total),
        "by_top_level": dict(sorted(by_top.items(), key=lambda kv: -kv[1]["bytes"])[:top_n]),
        "by_extension": dict(sorted(exts.items(), key=lambda kv: -kv[1])[:top_n]),
        "largest": [{"path": p, "bytes": s} for s, p in largest[:top_n]],
    }
