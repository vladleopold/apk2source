"""Shared helpers: logging, hashing, safe paths, process running."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

_CHUNK = 1 << 20


def log(msg: str, *, level: str = "INFO") -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def warn(msg: str) -> None:
    log(msg, level="WARN")


def err(msg: str) -> None:
    log(msg, level="ERROR")


def sha256_file(path: str | os.PathLike, chunk: int = _CHUNK) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


_UNSAFE = re.compile(r"[^A-Za-z0-9._\- ]+")


def slug(name: str, max_len: int = 80) -> str:
    """Filesystem / branch / repo-safe slug. Case and underscores preserved."""
    s = str(name).strip().replace("/", "-").replace("\\", "-")
    s = _UNSAFE.sub("", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return (s or "unnamed")[:max_len].strip("-.")


def safe_join(root: str | os.PathLike, *parts: str) -> Path:
    """Join and guarantee the result stays inside root (zip-slip guard)."""
    root = Path(root).resolve()
    target = (root.joinpath(*parts)).resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise ValueError(f"path escapes root: {target} !< {root}")
    return target


def ensure_dir(p: str | os.PathLike) -> Path:
    d = Path(p)
    d.mkdir(parents=True, exist_ok=True)
    return d


def run(
    cmd: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
    check: bool = True,
    capture: bool = True,
    stdin: Optional[bytes] = None,
) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    if env:
        e.update(env)
    log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(
        [str(c) for c in cmd],
        cwd=cwd,
        env=e,
        timeout=timeout,
        check=check,
        input=stdin,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )


def which(*names: str) -> Optional[str]:
    from shutil import which as _w

    for n in names:
        p = _w(n)
        if p:
            return p
    return None


def write_json(path: str | os.PathLike, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=False, default=str), encoding="utf-8")


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def walk_files(root: str | os.PathLike, exts: Optional[Iterable[str]] = None) -> Iterable[Path]:
    root = Path(root)
    if not root.exists():
        return
    exts = tuple(e.lower() for e in exts) if exts else None
    for p in sorted(root.rglob("*")):
        if p.is_file() and (exts is None or p.suffix.lower() in exts):
            yield p


def dir_size(root: str | os.PathLike) -> int:
    return sum(p.stat().st_size for p in walk_files(root))


def gh_output(name: str, value: str) -> None:
    """Append to $GITHUB_OUTPUT when running inside Actions."""
    f = os.environ.get("GITHUB_OUTPUT")
    if not f:
        return
    delim = f"EOF_{int(time.time()*1000)}"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def gh_summary(md: str) -> None:
    f = os.environ.get("GITHUB_STEP_SUMMARY")
    if not f:
        return
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(md + "\n")


def in_actions() -> bool:
    return bool(os.environ.get("GITHUB_ACTIONS"))


def fail(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    err(msg)
    sys.exit(code)
