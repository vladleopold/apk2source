"""Stage 05 - SPINE EXTRACTION.

Content-based discovery and reconstruction of Spine skeleton data from an
extracted asset tree. Detection never trusts filenames: everything is sniffed
from bytes, so obfuscated / renamed / repacked builds still resolve.

Recognised payloads
  skeleton json   TextAsset whose body parses as JSON with skeleton+bones+slots
  skeleton binary TextAsset whose body matches the Spine .skel header shape
  atlas           TextAsset whose body matches the Spine atlas page grammar
  texture pages   PNG images referenced by an atlas (or co-located by name)

Output (per game):
    <game>/<skeleton>/<skeleton>.json|.skel.bytes
    <game>/<skeleton>/<skeleton>.atlas
    <game>/<skeleton>/<page>.png
    <game>/<skeleton>/_meta.json
    <game>/_index.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .util import ensure_dir, human, log, safe_join, sha256_bytes, slug, warn, write_json

SPINE_VERSION_RE = re.compile(rb"\b([234])\.([0-9]{1,2})(\.[0-9]{1,3})?\b")
ATLAS_SIZE_RE = re.compile(r"^size:\s*(\d+)\s*,\s*(\d+)\s*$")
ATLAS_KEY_RE = re.compile(r"^(size|format|filter|repeat|pma|rotate|xy|orig|offset|index|bounds)\s*:")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JSON_EXTS = {".json", ".txt", ".bytes", ".skel", ".atlas", ""}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tga", ".bmp"}


# ==========================================================================
# detectors
# ==========================================================================
def looks_like_png(b: bytes) -> bool:
    return b[:8] == PNG_MAGIC


def detect_skeleton_json(b: bytes) -> Optional[Dict[str, Any]]:
    """Return spine metadata if `b` is a Spine skeleton .json."""
    if not b or b[0:1] not in (b"{", b"[", b" ", b"\n", b"\r", b"\t"):
        return None
    if len(b) > 64 * 1024 * 1024:
        return None
    try:
        doc = json.loads(b.decode("utf-8", "strict"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(doc, dict):
        return None
    sk = doc.get("skeleton")
    if not isinstance(sk, dict):
        return None
    if "bones" not in doc or "slots" not in doc:
        return None
    version = str(sk.get("spine") or "")
    if not re.match(r"^[234]\.\d", version):
        # some exports omit skeleton.spine; require animations as a fallback signal
        if "animations" not in doc:
            return None
    return {
        "kind": "json",
        "spine_version": version,
        "bones": len(doc.get("bones") or []),
        "slots": len(doc.get("slots") or []),
        "skins": len(doc.get("skins") or []),
        "animations": sorted((doc.get("animations") or {}).keys()),
        "hash": sk.get("hash"),
        "width": sk.get("width"),
        "height": sk.get("height"),
        "fps": sk.get("fps"),
    }


def detect_skeleton_binary(b: bytes) -> Optional[Dict[str, Any]]:
    """Heuristic detector for Spine `.skel` / `.skel.bytes` binary skeletons.

    Spine's SkeletonBinary starts with two length-prefixed UTF-8 strings:
    the skeleton hash (may be null) and the format version, then two
    big-endian floats (width, height) and a nonessential flag.
    Length prefixes are varints where 0 == null and 1 == empty string.
    """
    if len(b) < 16:
        return None

    def read_str(pos: int) -> Tuple[Optional[str], int, bool]:
        n = 0
        shift = 0
        while pos < len(b):
            byte = b[pos]
            pos += 1
            n |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
            if shift > 28:
                return None, pos, False
        else:
            return None, pos, False
        if n == 0:
            return None, pos, True          # null string
        if n == 1:
            return "", pos, True            # empty string
        n -= 1
        if n > 512 or pos + n > len(b):
            return None, pos, False
        try:
            return b[pos:pos + n].decode("utf-8"), pos + n, True
        except Exception:  # noqa: BLE001
            return None, pos, False

    header_hash, pos, ok = read_str(0)
    if not ok:
        return None
    version, pos, ok = read_str(pos)
    if not ok or not version:
        return None
    if not re.match(r"^[234]\.\d+(\.\d+)?$", version):
        return None
    info: Dict[str, Any] = {"kind": "skel", "spine_version": version,
                            "header_hash": header_hash, "size": len(b)}
    if pos + 9 <= len(b):
        import struct as _st
        try:
            w, h = _st.unpack_from(">ff", b, pos)
            if 0 < abs(w) < 100000 and 0 < abs(h) < 100000:
                info["width"], info["height"] = round(w, 3), round(h, 3)
        except Exception:  # noqa: BLE001
            pass
    return info


def detect_atlas(b: bytes) -> Optional[Dict[str, Any]]:
    """Return atlas page info if `b` is a Spine atlas.

    Grammar (Spine 3.x/4.x):
        <page file>.png          <- not indented
        size: W,H                <- page props, not indented
        format: RGBA8888
        filter: Linear,Linear
        repeat: none
        <region name>            <- not indented
          rotate: false          <- region props, indented
          xy: 2, 2
          ...
        <blank line>             <- page separator
    """
    if not b or len(b) > 16 * 1024 * 1024:
        return None
    try:
        text = b.decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    if "\x00" in text:
        return None
    lines = [ln.rstrip("\r") for ln in text.split("\n")]

    pages: List[Dict[str, Any]] = []
    regions_total = 0
    i = 0
    while i < len(lines):
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            break
        header = lines[i].strip()
        if not re.search(r"\.(png|jpg|jpeg|webp)$", header, re.I):
            return None
        i += 1

        props: Dict[str, Any] = {"file": header}
        page_keys = 0
        while i < len(lines) and lines[i].strip() and not lines[i][:1].isspace():
            s_ = lines[i].strip()
            m = ATLAS_KEY_RE.match(s_)
            if not m:
                break
            k, v = s_.split(":", 1)
            k, v = k.strip(), v.strip()
            props[k] = v
            sm = ATLAS_SIZE_RE.match(s_)
            if sm:
                props["width"], props["height"] = int(sm.group(1)), int(sm.group(2))
            page_keys += 1
            i += 1
        if page_keys < 1 or "size" not in props:
            return None

        regions = 0
        while i < len(lines):
            ln = lines[i]
            if not ln.strip():
                i += 1
                break
            if ln[:1].isspace():
                i += 1
                continue
            regions += 1
            i += 1
            while i < len(lines) and lines[i].strip() and lines[i][:1].isspace():
                i += 1
        props["regions"] = regions
        regions_total += regions
        pages.append(props)

    if not pages:
        return None
    return {"kind": "atlas", "pages": pages, "regions": regions_total, "bytes": len(b)}


# ==========================================================================
# records
# ==========================================================================
@dataclass
class Candidate:
    path: Path
    rel: str
    kind: str                      # json | skel | atlas | image
    data: bytes = b""
    info: Dict[str, Any] = field(default_factory=dict)
    origin: Dict[str, Any] = field(default_factory=dict)

    @property
    def stem(self) -> str:
        p = Path(self.rel)
        name = p.name
        for suf in (".atlas.txt", ".atlas", ".skel.bytes", ".skel", ".json", ".txt", ".bytes",
                    ".png", ".jpg", ".jpeg", ".webp"):
            if name.lower().endswith(suf):
                return name[: -len(suf)]
        return p.stem


@dataclass
class Skeleton:
    name: str
    json: Optional[Candidate] = None
    skel: Optional[Candidate] = None
    atlases: List[Candidate] = field(default_factory=list)
    textures: List[Candidate] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return bool((self.json or self.skel) and self.textures)


# ==========================================================================
# tree scan
# ==========================================================================
SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".tga", ".bmp", ".gif", ".psd",
    ".wav", ".mp3", ".ogg", ".opus", ".flac", ".aif", ".aiff", ".m4a",
    ".mp4", ".webm", ".mov", ".avi", ".mkv",
    ".ttf", ".otf", ".woff", ".woff2",
    ".so", ".dll", ".dylib", ".a", ".o", ".class", ".dex", ".jar",
    ".zip", ".gz", ".bz2", ".7z", ".rar", ".lz4", ".bundle", ".assets",
    ".unity3d", ".resS", ".resource", ".fbx", ".obj", ".blend",
    ".mp4", ".bnk", ".acb", ".awb",
}


def scan_tree(root: Path, *, max_bytes: int = 200 * 1024 * 1024,
              sniff_extensionless: bool = True) -> List[Candidate]:
    """Walk an extracted asset tree and classify every Spine-relevant file."""
    root = Path(root)
    out: List[Candidate] = []
    seen_hashes: Dict[str, Candidate] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0 or size > max_bytes:
            continue
        rel = p.relative_to(root).as_posix()
        ext = p.suffix.lower()

        if ext in IMAGE_EXTS:
            try:
                head = p.open("rb").read(8)
            except OSError:
                continue
            if looks_like_png(head) or ext != ".png":
                c = Candidate(p, rel, "image", info={"bytes": size})
                out.append(c)
            continue

        if ext in SKIP_EXTS:
            continue
        if ext and not sniff_extensionless and ext not in JSON_EXTS:
            continue

        try:
            data = p.read_bytes()
        except OSError:
            continue

        kind = info = None
        j = detect_skeleton_json(data)
        if j:
            kind, info = "json", j
        else:
            a = detect_atlas(data)
            if a:
                kind, info = "atlas", a
            else:
                k = detect_skeleton_binary(data)
                if k:
                    kind, info = "skel", k
        if not kind:
            continue

        # content-level dedupe: identical payloads shipped in several bundles
        h = sha256_bytes(data)
        prev = seen_hashes.get((kind, h))
        if prev is not None:
            prev.info.setdefault("duplicate_of", []).append(rel)
            prev.origin.setdefault("duplicates", []).append(rel)
            continue
        c = Candidate(p, rel, kind, data, info)
        c.origin = {"sha256": h, "bytes": len(data)}
        seen_hashes[(kind, h)] = c
        out.append(c)

    # images: dedupe by content too, but keep every distinct path for pairing
    img_hashes: Dict[str, str] = {}
    for c in list(out):
        if c.kind != "image":
            continue
        try:
            h = sha256_bytes(c.path.read_bytes())
        except OSError:
            continue
        c.origin = {"sha256": h, "bytes": c.info.get("bytes")}
        img_hashes.setdefault(h, c.rel)
    return out


def group(cands: List[Candidate]) -> List[Skeleton]:
    """Pair skeletons with atlases and texture pages.

    Matching precedence:
      1. atlas page filename -> texture filename (exact)
      2. skeleton stem == atlas stem (exact)
      3. skeleton stem == texture stem (exact)
      4. single-skeleton folder -> every image in that folder
    """
    images = [c for c in cands if c.kind == "image"]
    by_dir: Dict[str, List[Candidate]] = {}
    for c in cands:
        by_dir.setdefault(str(Path(c.rel).parent), []).append(c)

    # global filename -> image index (atlases often reference a bare filename)
    by_basename: Dict[str, List[Candidate]] = {}
    for im in images:
        by_basename.setdefault(Path(im.rel).name.lower(), []).append(im)
        by_basename.setdefault(im.stem.lower(), []).append(im)

    skeletons: List[Skeleton] = []
    for d, items in sorted(by_dir.items()):
        skels = [c for c in items if c.kind in ("json", "skel")]
        atlases = [c for c in items if c.kind == "atlas"]
        local_images = [c for c in items if c.kind == "image"]
        local_img_names = {Path(i.rel).name.lower(): i for i in local_images}
        local_img_names.update({i.stem.lower(): i for i in local_images})

        taken: set = set()

        def pick_page(fn: str) -> Optional[Candidate]:
            key = Path(fn).name.lower()
            hit = local_img_names.get(key) or local_img_names.get(Path(key).stem)
            if hit is None:
                for cand in by_basename.get(key, []):
                    hit = cand
                    break
            if hit is not None and hit.rel not in taken:
                taken.add(hit.rel)
                return hit
            return hit if hit is not None else None

        for sk in skels:
            name = sk.stem
            s = Skeleton(name=name)
            if sk.kind == "json":
                s.json = sk
            else:
                s.skel = sk
            s.meta = dict(sk.info)
            s.meta["source_rel"] = sk.rel
            s.meta["source_path"] = str(sk.path)
            s.meta["source_sha256"] = (sk.origin or {}).get("sha256")

            exact = [a for a in atlases if a.stem == name]
            if not exact:
                exact = [a for a in atlases if a.stem.lower().startswith(name.lower() + ".")
                         or a.stem.lower().startswith(name.lower() + "_")]
            if not exact and len(skels) == 1:
                exact = atlases
            s.atlases = exact

            texs: List[Candidate] = []
            for a in exact:
                for page in a.info.get("pages", []):
                    hit = pick_page(page["file"])
                    if hit is not None and hit.rel not in {t.rel for t in texs}:
                        texs.append(hit)
            if not texs:
                for im in local_images:
                    if im.stem == name and im.rel not in taken:
                        taken.add(im.rel)
                        texs.append(im)
            if not texs and len(skels) == 1 and len(local_images) >= 1 and not exact:
                texs = list(local_images)
            s.textures = texs
            skeletons.append(s)

        # json + skel of the same character -> merge into one entry
        skeletons = _merge_formats(skeletons)

        # orphan atlases (no skeleton in the folder) are still worth keeping
        claimed = {a.rel for s in skeletons for a in s.atlases}
        for a in atlases:
            if a.rel in claimed:
                continue
            s = Skeleton(name=a.stem, atlases=[a])
            s.meta = dict(a.info)
            s.meta["source_rel"] = a.rel
            s.meta["orphan_atlas"] = True
            for page in a.info.get("pages", []):
                hit = local_img_names.get(Path(page["file"]).name.lower())
                if hit:
                    s.textures.append(hit)
            skeletons.append(s)

    _disambiguate(skeletons)
    return skeletons


def _merge_formats(skeletons: List[Skeleton]) -> List[Skeleton]:
    """Fold `<name>` (json) and `<name>_SkeletonData`/`<name>.skel` into one."""
    out: List[Skeleton] = []
    by_key: Dict[str, Skeleton] = {}
    for s in skeletons:
        key = re.sub(r"(_skeletondata|_skel|skeletondata|[-_.]?skel)$", "", s.name.lower())
        key = re.sub(r"[^a-z0-9]+", "", key)
        twin = by_key.get(key)
        if twin is None:
            by_key[key] = s
            out.append(s)
            continue
        if twin.json and s.json is None and s.skel:
            twin.skel = s.skel
            twin.meta.setdefault("skel_source_rel", s.meta.get("source_rel"))
        elif twin.skel and s.skel is None and s.json:
            twin.json = s.json
            twin.meta.setdefault("json_source_rel", s.meta.get("source_rel"))
        else:
            by_key[key + str(len(out))] = s
            out.append(s)
    return out


def _disambiguate(skeletons: List[Skeleton]) -> None:
    """Make names unique, using the originating bundle/dir as the qualifier."""
    seen: Dict[str, int] = {}
    for s in skeletons:
        base = s.name
        if base not in seen:
            seen[base] = 1
            continue
        seen[base] += 1
        src = Path(s.meta.get("source_rel") or "")
        qualifier = ""
        for part in src.parts[:-1]:
            if part not in (".", "TextAsset", "Texture2D", "spine", "assets"):
                qualifier = part
        s.name = f"{base}~{qualifier or seen[base]}"


# ==========================================================================
# export
# ==========================================================================
def export(skeletons: List[Skeleton], out_dir: Path, game: str,
           *, provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    game_slug = slug(game)
    root = ensure_dir(Path(out_dir) / game_slug)
    index: List[Dict[str, Any]] = []
    stats = {"skeletons": 0, "json": 0, "skel": 0, "atlas": 0, "textures": 0,
             "bytes": 0, "complete": 0, "incomplete": 0}

    for s in skeletons:
        d = ensure_dir(root / slug(s.name))
        entry: Dict[str, Any] = {
            "name": s.name,
            "dir": str(d.relative_to(root.parent)),
            "files": [],
            "spine_version": s.meta.get("spine_version"),
            "animations": s.meta.get("animations", []),
            "bones": s.meta.get("bones"),
            "slots": s.meta.get("slots"),
            "complete": s.complete,
        }

        if s.json:
            fn = f"{slug(s.name)}.json"
            (d / fn).write_bytes(s.json.data)
            entry["files"].append({"role": "skeleton_json", "name": fn,
                                   "sha256": sha256_bytes(s.json.data), "bytes": len(s.json.data)})
            entry["skeleton_format"] = "json"
            stats["json"] += 1
        if s.skel:
            fn = f"{slug(s.name)}.skel.bytes"
            (d / fn).write_bytes(s.skel.data)
            entry["files"].append({"role": "skeleton_binary", "name": fn,
                                   "sha256": sha256_bytes(s.skel.data), "bytes": len(s.skel.data)})
            entry.setdefault("skeleton_format", "skel")
            stats["skel"] += 1

        for i, a in enumerate(s.atlases):
            body = _rewrite_atlas(a.data, s)
            fn = f"{slug(s.name)}.atlas" if len(s.atlases) == 1 else f"{slug(s.name)}_{i}.atlas"
            (d / fn).write_bytes(body)
            entry["files"].append({"role": "atlas", "name": fn,
                                   "sha256": sha256_bytes(body), "bytes": len(body)})
            stats["atlas"] += 1

        for t in s.textures:
            ext = Path(t.rel).suffix.lower() or ".png"
            data = t.data or t.path.read_bytes()
            fn = Path(t.rel).name
            fn = fn if fn.lower().endswith(tuple(IMAGE_EXTS)) else f"{slug(t.stem)}{ext}"
            try:
                dest = safe_join(d, fn)
            except ValueError:
                dest = d / slug(fn)
            dest.write_bytes(data)
            entry["files"].append({"role": "texture", "name": dest.name,
                                   "sha256": sha256_bytes(data), "bytes": len(data)})
            stats["textures"] += 1
            stats["bytes"] += len(data)

        entry["provenance"] = {
            "source_rel": s.meta.get("source_rel"),
            "source_sha256": s.meta.get("source_sha256"),
            "origin": s.meta.get("origin") or {},
        }
        write_json(d / "_meta.json", {**entry, "game": game_slug, "pipeline": provenance or {}})
        index.append(entry)
        stats["skeletons"] += 1
        stats["complete" if s.complete else "incomplete"] += 1

    write_json(root / "_index.json", {
        "game": game_slug,
        "generated_by": "apk2source",
        "provenance": provenance or {},
        "stats": stats,
        "skeletons": index,
    })
    log(f"spine export: {stats['skeletons']} skeletons "
        f"({stats['complete']} complete / {stats['incomplete']} incomplete), "
        f"{stats['textures']} textures, {human(stats['bytes'])}")
    return {"stage": "spine_extract", "game": game_slug, "out": str(root), "stats": stats,
            "skeletons": index}


def _rewrite_atlas(data: bytes, s: Skeleton) -> bytes:
    """Normalise page filenames in the atlas to the names we actually wrote."""
    if not s.textures:
        return data
    try:
        text = data.decode("utf-8")
    except Exception:  # noqa: BLE001
        return data
    names = {Path(t.rel).name for t in s.textures}
    lines = text.split("\n")
    out: List[str] = []
    for ln in lines:
        st = ln.strip()
        if st and not ln[:1].isspace() and re.search(r"\.(png|jpg|jpeg|webp)$", st, re.I):
            base = Path(st).stem
            match = next((n for n in names if Path(n).stem == base), None)
            out.append(match or st)
        else:
            out.append(ln)
    return "\n".join(out).encode("utf-8")


def run(assets_root: Path, out_dir: Path, game: str,
        provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    log(f"scanning {assets_root} for spine payloads")
    cands = scan_tree(Path(assets_root))
    kinds: Dict[str, int] = {}
    for c in cands:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    log(f"candidates: {kinds}")
    skeletons = group(cands)
    return export(skeletons, Path(out_dir), game, provenance=provenance)
