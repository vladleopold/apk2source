"""Stage 01 - ACQUIRE.

Downloads an *.apk / *.apks / *.xapk / *.apkm / *.aab from a URL into a local
cache directory. Supports:

  * HTTP range resume (large multi-GB files survive runner restarts)
  * sha256 verification + a sidecar `.meta.json` cache record
  * `--cache-dir` reuse so repeated pipeline runs skip the download
  * mirrors / fallback URLs
  * explicit "no cache" mode

The cache record is what makes the "with cache / without cache" switch in the
control panel work: a hit is decided by (url, sha256|size) -> cached path.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .util import ensure_dir, human, log, read_json, sha256_file, slug, warn, write_json

UA = "apk2source/1.0 (+https://github.com/leaopold/apk2source)"
META_SUFFIX = ".meta.json"
CONTAINER_EXTS = (".apk", ".apks", ".xapk", ".apkm", ".aab", ".zip")


def _name_from_url(url: str) -> str:
    """Best-effort payload filename for a download URL.

    Plain links carry the filename in the path, but proxy URLs such as the
    Worker R2 endpoint (/api/download?key=uploads/<game>/<ts>/name.apks)
    end in a bare segment like "download". For those, recover the real name
    from the ?key= query param so downstream jobs can find the payload by
    its container extension.
    """
    parsed = urllib.parse.urlparse(url)
    base = Path(parsed.path).name or ""
    if base.lower().endswith(CONTAINER_EXTS):
        return base
    qs = urllib.parse.parse_qs(parsed.query)
    for v in qs.get("key", []):
        cand = Path(v).name
        if cand.lower().endswith(CONTAINER_EXTS):
            return cand
    return base or "payload.bin"


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + META_SUFFIX)


def cache_key(url: str) -> str:
    return slug(urllib.parse.urlparse(url).path.split("/")[-1] or "download") + "-" + slug(
        url.split("?")[0][-40:]
    )


def lookup_cache(cache_dir: Path, url: str, sha256: Optional[str] = None) -> Optional[Path]:
    """Return a cached payload path if a valid record exists."""
    if not cache_dir.exists():
        return None
    for meta in cache_dir.rglob("*" + META_SUFFIX):
        rec = read_json(meta, {}) or {}
        if rec.get("url") != url:
            continue
        payload = meta.with_name(meta.name[: -len(META_SUFFIX)])
        if not payload.exists():
            continue
        if sha256 and rec.get("sha256") != sha256:
            continue
        if rec.get("complete") and payload.stat().st_size == rec.get("size"):
            log(f"cache HIT: {payload} ({human(payload.stat().st_size)})")
            return payload
    return None


def _request(url: str, headers: Dict[str, str], timeout: int) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - explicit user-supplied URL


def head_info(url: str, timeout: int = 60) -> Dict[str, Any]:
    info: Dict[str, Any] = {"url": url}
    if url.startswith("file://"):
        src = Path(urllib.request.url2pathname(urllib.parse.urlparse(url).path))
        info["size"] = src.stat().st_size if src.exists() else None
        info["accepts_ranges"] = False
        return info
    try:
        with _request(url, {"Range": "bytes=0-0"}, timeout) as r:
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                info["size"] = int(cr.split("/")[-1])
                info["accepts_ranges"] = True
            else:
                cl = r.headers.get("Content-Length")
                info["size"] = int(cl) if cl else None
                info["accepts_ranges"] = False
            info["content_type"] = r.headers.get("Content-Type")
            info["final_url"] = r.geturl()
    except urllib.error.HTTPError as e:  # some servers reject Range
        if e.code == 416:
            info["accepts_ranges"] = True
        else:
            info["error"] = f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)
    return info


def download(
    url: str,
    dest: Path,
    *,
    timeout: int = 120,
    chunk: int = 1 << 20,
    resume: bool = True,
    max_retries: int = 5,
    progress_every: float = 10.0,
) -> Path:
    """Download with resume + retries. Returns dest."""
    ensure_dir(dest.parent)
    if url.startswith("file://"):
        src = Path(urllib.request.url2pathname(urllib.parse.urlparse(url).path))
        shutil.copyfile(src, dest)
        log(f"copied local file {src} -> {dest}")
        return dest
    part = dest.with_name(dest.name + ".part")
    attempt = 0
    while True:
        attempt += 1
        offset = part.stat().st_size if (resume and part.exists()) else 0
        headers: Dict[str, str] = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            log(f"resuming from {human(offset)}")
        try:
            with _request(url, headers, timeout) as r:
                if offset and r.status not in (206, 200):
                    raise IOError(f"unexpected status {r.status} on resume")
                mode = "ab" if (offset and r.status == 206) else "wb"
                if mode == "wb":
                    offset = 0
                total = r.headers.get("Content-Length")
                total = (int(total) + offset) if total else None
                done = offset
                last = time.time()
                with open(part, mode) as f:
                    while True:
                        b = r.read(chunk)
                        if not b:
                            break
                        f.write(b)
                        done += len(b)
                        if time.time() - last >= progress_every:
                            pct = f"{100*done/total:.1f}%" if total else "?"
                            log(f"  {human(done)} / {human(total) if total else '?'} ({pct})")
                            last = time.time()
            if total and done < total:
                raise IOError(f"short read: {done} < {total}")
            break
        except Exception as e:  # noqa: BLE001
            if attempt > max_retries:
                raise
            wait = min(60, 5 * 2 ** (attempt - 1))
            warn(f"download attempt {attempt} failed: {e}; retry in {wait}s")
            time.sleep(wait)
    part.replace(dest)
    return dest


def acquire(
    urls: List[str],
    out_dir: Path,
    cache_dir: Optional[Path] = None,
    *,
    use_cache: bool = True,
    sha256: Optional[str] = None,
    filename: Optional[str] = None,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Acquire a payload from the first working URL. Returns a stage record."""
    started = time.time()
    out_dir = ensure_dir(out_dir)
    errors: List[str] = []

    if use_cache and cache_dir:
        for u in urls:
            hit = lookup_cache(Path(cache_dir), u, sha256)
            if hit:
                name = filename or hit.name
                if not name.lower().endswith(CONTAINER_EXTS):
                    # Old cache entries may carry an extensionless name
                    # (e.g. "download" from a proxy URL) — recover it.
                    fixed = _name_from_url(u)
                    if fixed.lower().endswith(CONTAINER_EXTS):
                        name = fixed
                dest = out_dir / name
                if dest.resolve() != hit.resolve():
                    shutil.copy2(hit, dest)
                return _record(dest, u, started, cached=True, sha256=sha256)

    for u in urls:
        name = filename or _name_from_url(u)
        dest = out_dir / name
        try:
            log(f"downloading {u}")
            download(u, dest, timeout=timeout)
            rec = _record(dest, u, started, cached=False, sha256=sha256)
            if sha256 and rec["sha256"] != sha256:
                raise IOError(f"sha256 mismatch: got {rec['sha256']} want {sha256}")
            if use_cache and cache_dir:
                _store_cache(Path(cache_dir), u, dest, rec)
            return rec
        except Exception as e:  # noqa: BLE001
            errors.append(f"{u}: {e}")
            warn(errors[-1])

    raise RuntimeError("all download sources failed:\n  " + "\n  ".join(errors))


def _record(dest: Path, url: str, started: float, *, cached: bool, sha256: Optional[str]) -> Dict[str, Any]:
    size = dest.stat().st_size
    digest = sha256 or sha256_file(dest)
    rec = {
        "stage": "acquire",
        "path": str(dest),
        "name": dest.name,
        "url": url,
        "size": size,
        "size_human": human(size),
        "sha256": digest,
        "cached": cached,
        "seconds": round(time.time() - started, 2),
    }
    write_json(dest.with_name(dest.name + ".acquire.json"), rec)
    log(f"acquired {dest.name} {human(size)} sha256={digest[:16]}... cached={cached}")
    return rec


def _store_cache(cache_dir: Path, url: str, dest: Path, rec: Dict[str, Any]) -> None:
    try:
        bucket = ensure_dir(cache_dir / cache_key(url)[:2])
        target = bucket / dest.name
        if not target.exists():
            shutil.copy2(dest, target)
        write_json(_meta_path(target), {**rec, "complete": True, "path": str(target), "cached_at": time.time()})
        log(f"cache STORE: {target}")
    except Exception as e:  # noqa: BLE001
        warn(f"cache store failed: {e}")


def purge_cache(cache_dir: Path) -> int:
    n = 0
    if cache_dir.exists():
        n = sum(1 for _ in cache_dir.rglob("*") if _.is_file())
        shutil.rmtree(cache_dir, ignore_errors=True)
    return n
