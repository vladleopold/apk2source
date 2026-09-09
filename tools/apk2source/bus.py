"""Artifact bus - moves multi-GB payloads between pipeline stages.

GitHub Actions artifacts/caches cannot hold a 2.8 GB split-APK set (public repos
get 1 GB of artifact storage), so the bus defaults to an S3-compatible object
store (Cloudflare R2 / MinIO / AWS S3) and degrades gracefully:

    backend=s3     R2/S3 over SigV4, single PUT up to 5 GB, ranged GET resume
    backend=local  a plain directory (self-hosted runner / local dev)
    backend=none   no-op, every stage re-downloads from the origin URL

SigV4 is implemented here directly so the pipeline needs no boto3 wheel, which
keeps the CI image small and the dependency surface auditable.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .util import ensure_dir, human, log, warn, write_json

EMPTY_SHA = hashlib.sha256(b"").hexdigest()
CHUNK = 8 * 1024 * 1024


class BusError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# SigV4
# --------------------------------------------------------------------------
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _sigv4_headers(method: str, url: str, payload_hash: str, *, access_key: str,
                   secret_key: str, region: str, service: str = "s3",
                   extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    canonical_uri = urllib.parse.quote(parts.path or "/", safe="/~")
    canonical_qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    )
    now = datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amzdate}
    if extra:
        headers.update({k.lower(): v for k, v in extra.items()})
    signed_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_qs, canonical_headers, signed_names, payload_hash]
    )
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amzdate, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    k = _sign(("AWS4" + secret_key).encode(), datestamp)
    k = _sign(k, region)
    k = _sign(k, service)
    k = _sign(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k: v for k, v in headers.items() if k != "host"}
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    return out


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------
class Bus:
    name = "base"

    def put(self, src: Path, key: str) -> Dict[str, Any]:
        raise NotImplementedError

    def get(self, key: str, dest: Path) -> Path:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        raise NotImplementedError

    def delete(self, key: str) -> bool:
        raise NotImplementedError

    def url(self, key: str) -> Optional[str]:
        return None


class NullBus(Bus):
    name = "none"

    def put(self, src, key):
        warn("bus=none: not storing anything")
        return {"stored": False}

    def get(self, key, dest):
        raise BusError("bus=none: nothing to fetch")

    def exists(self, key):
        return False

    def list(self, prefix=""):
        return []

    def delete(self, key):
        return False


class LocalBus(Bus):
    name = "local"

    def __init__(self, root: Path):
        self.root = ensure_dir(root)

    def _p(self, key: str) -> Path:
        k = key.lstrip("/")
        target = (self.root / k).resolve()
        if os.path.commonpath([str(self.root.resolve()), str(target)]) != str(self.root.resolve()):
            raise BusError(f"key escapes bus root: {key}")
        return target

    def put(self, src: Path, key: str) -> Dict[str, Any]:
        dest = self._p(key)
        ensure_dir(dest.parent)
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copyfile(src, tmp)
        tmp.replace(dest)
        return {"stored": True, "key": key, "path": str(dest), "bytes": dest.stat().st_size}

    def get(self, key: str, dest: Path) -> Path:
        src = self._p(key)
        if not src.exists():
            raise BusError(f"missing key: {key}")
        ensure_dir(Path(dest).parent)
        shutil.copyfile(src, dest)
        return Path(dest)

    def exists(self, key: str) -> bool:
        return self._p(key).exists()

    def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        base = self._p(prefix) if prefix else self.root
        if not base.exists():
            return []
        root = base if base.is_dir() else base.parent
        return [
            {"key": p.relative_to(self.root).as_posix(), "bytes": p.stat().st_size,
             "modified": p.stat().st_mtime}
            for p in sorted(root.rglob("*")) if p.is_file()
        ]

    def delete(self, key: str) -> bool:
        p = self._p(key)
        if p.exists():
            p.unlink()
            return True
        return False


class S3Bus(Bus):
    """S3-compatible object store (Cloudflare R2, MinIO, AWS S3)."""

    name = "s3"

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str,
                 region: str = "auto", prefix: str = ""):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "auto"
        self.prefix = prefix.strip("/")
        # path-style is what R2/MinIO expect
        self.base = f"{self.endpoint}/{self.bucket}"

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key.lstrip('/')}" if self.prefix else key.lstrip("/")

    def _url(self, key: str) -> str:
        return f"{self.base}/{urllib.parse.quote(self._key(key), safe='/')}"

    def url(self, key: str) -> Optional[str]:
        return self._url(key)

    def _req(self, method: str, key: str, *, data: Optional[bytes] = None,
             payload_hash: str = EMPTY_SHA, extra: Optional[Dict[str, str]] = None,
             timeout: int = 300) -> urllib.request.Request:
        url = self._url(key)
        headers = _sigv4_headers(method, url, payload_hash, access_key=self.access_key,
                                 secret_key=self.secret_key, region=self.region, extra=extra)
        return urllib.request.Request(url, data=data, method=method, headers=headers)

    def exists(self, key: str) -> bool:
        try:
            with urllib.request.urlopen(self._req("HEAD", key), timeout=60):  # noqa: S310
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise BusError(f"HEAD {key}: HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001
            raise BusError(f"HEAD {key}: {e}") from e

    def head(self, key: str) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(self._req("HEAD", key), timeout=60) as r:  # noqa: S310
                return {"bytes": int(r.headers.get("Content-Length") or 0),
                        "etag": (r.headers.get("ETag") or "").strip('"'),
                        "last_modified": r.headers.get("Last-Modified")}
        except Exception as e:  # noqa: BLE001
            raise BusError(f"HEAD {key}: {e}") from e

    def put(self, src: Path, key: str) -> Dict[str, Any]:
        src = Path(src)
        size = src.stat().st_size
        if size > 5 * 1024 ** 3:
            raise BusError(f"{human(size)} exceeds the 5 GiB single-PUT limit; split the payload")
        digest = hashlib.sha256()
        with open(src, "rb") as f:
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                digest.update(b)
        payload_hash = digest.hexdigest()
        started = time.time()
        with open(src, "rb") as f:
            req = self._req("PUT", key, data=f, payload_hash=payload_hash,
                            extra={"content-length": str(size)}, timeout=7200)
            try:
                with urllib.request.urlopen(req, timeout=7200) as r:  # noqa: S310
                    etag = (r.headers.get("ETag") or "").strip('"')
            except urllib.error.HTTPError as e:
                raise BusError(f"PUT {key}: HTTP {e.code} {e.read()[:400]!r}") from e
        rec = {"stored": True, "key": self._key(key), "bytes": size, "sha256": payload_hash,
               "etag": etag, "seconds": round(time.time() - started, 1)}
        log(f"bus.put {key} {human(size)} in {rec['seconds']}s")
        return rec

    def get(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        ensure_dir(dest.parent)
        part = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(1, 6):
            offset = part.stat().st_size if part.exists() else 0
            extra = {"range": f"bytes={offset}-"} if offset else None
            try:
                req = self._req("GET", key, payload_hash=EMPTY_SHA, extra=extra, timeout=7200)
                with urllib.request.urlopen(req, timeout=7200) as r:  # noqa: S310
                    mode = "ab" if offset and r.status == 206 else "wb"
                    if mode == "wb":
                        offset = 0
                    total = r.headers.get("Content-Length")
                    total = int(total) + offset if total else None
                    done = offset
                    last = time.time()
                    with open(part, mode) as f:
                        while True:
                            b = r.read(CHUNK)
                            if not b:
                                break
                            f.write(b)
                            done += len(b)
                            if time.time() - last >= 15:
                                log(f"  bus.get {human(done)}/{human(total) if total else '?'}")
                                last = time.time()
                part.replace(dest)
                log(f"bus.get {key} -> {dest} ({human(dest.stat().st_size)})")
                return dest
            except Exception as e:  # noqa: BLE001
                if attempt >= 5:
                    raise BusError(f"GET {key}: {e}") from e
                warn(f"bus.get attempt {attempt} failed: {e}; retrying")
                time.sleep(5 * attempt)
        raise BusError(f"GET {key}: exhausted retries")

    def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        """List via the S3 XML API (no external deps)."""
        import xml.etree.ElementTree as ET

        full = self._key(prefix) if prefix else self.prefix
        url = f"{self.base}?list-type=2&prefix={urllib.parse.quote(full)}&max-keys=1000"
        headers = _sigv4_headers("GET", url, EMPTY_SHA, access_key=self.access_key,
                                 secret_key=self.secret_key, region=self.region)
        out: List[Dict[str, Any]] = []
        try:
            with urllib.request.urlopen(  # noqa: S310
                urllib.request.Request(url, headers=headers), timeout=120
            ) as r:
                root = ET.fromstring(r.read())
        except Exception as e:  # noqa: BLE001
            raise BusError(f"LIST {prefix}: {e}") from e
        ns = {"s3": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        for c in root.findall("s3:Contents", ns) if ns else root.iter():
            if not c.tag.endswith("Contents"):
                continue
            key = c.findtext("s3:Key", default="", namespaces=ns) if ns else c.findtext("Key", "")
            size = c.findtext("s3:Size", default="0", namespaces=ns) if ns else c.findtext("Size", "0")
            if key:
                out.append({"key": key, "bytes": int(size or 0)})
        return out

    def delete(self, key: str) -> bool:
        try:
            with urllib.request.urlopen(self._req("DELETE", key), timeout=120):  # noqa: S310
                return True
        except Exception as e:  # noqa: BLE001
            warn(f"bus.delete {key}: {e}")
            return False


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------
def from_env(backend: Optional[str] = None) -> Bus:
    backend = (backend or os.environ.get("APK2SOURCE_BUS") or "none").lower()
    if backend in ("local", "dir"):
        return LocalBus(Path(os.environ.get("APK2SOURCE_BUS_LOCAL", "./.bus")))
    if backend in ("s3", "r2"):
        endpoint = os.environ.get("APK2SOURCE_S3_ENDPOINT")
        bucket = os.environ.get("APK2SOURCE_S3_BUCKET")
        ak = os.environ.get("APK2SOURCE_S3_ACCESS_KEY_ID")
        sk = os.environ.get("APK2SOURCE_S3_SECRET_ACCESS_KEY")
        if not all([endpoint, bucket, ak, sk]):
            warn("bus=s3 requested but S3 env vars are incomplete -> falling back to none")
            return NullBus()
        return S3Bus(endpoint, bucket, ak, sk,
                     region=os.environ.get("APK2SOURCE_S3_REGION", "auto"),
                     prefix=os.environ.get("APK2SOURCE_S3_PREFIX", "apk2source"))
    return NullBus()


def key_for(game: str, stage: str, name: str) -> str:
    from .util import slug

    return f"{slug(game).lower()}/{stage}/{slug(name).lower()}"
