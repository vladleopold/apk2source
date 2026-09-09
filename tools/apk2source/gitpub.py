"""Stage 06 - PUBLISH.

Pushes pipeline output into the destination repositories:

    source_spine/<game-slug>/<skeleton>/{*.json,*.atlas,*.png,_meta.json}
    game_source/<game-slug>/...          (reconstructed Unity project)

Uses a partial clone (`--filter=blob:none` + sparse-checkout) so publishing a
single game does not download the whole history of a multi-GB source repo.
Large binaries can be routed through Git LFS automatically.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .util import ensure_dir, human, log, run, sha256_file, slug, warn, write_json

GIT = "git"
DEFAULT_LFS_THRESHOLD = 8 * 1024 * 1024
LFS_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.tga", "*.psd", "*.bundle",
                "*.assets", "*.unity3d", "*.skel.bytes", "*.wav", "*.mp3", "*.ogg",
                "*.mp4", "*.fbx", "*.obj", "*.zip", "*.aab", "*.apk")


def auth_url(repo: str, token: Optional[str]) -> str:
    """repo: 'owner/name', a full https URL, or a local path (tests/offline)."""
    if repo.startswith(("http://", "https://", "ssh://", "git@")):
        base = repo
    elif repo.startswith("/") or repo.startswith("file:") or os.path.isdir(repo):
        base = repo
    else:
        base = f"https://github.com/{repo.strip('/')}.git"
    if not token:
        return base
    m = re.match(r"(https://)(.*)", base)
    if not m:
        return base
    return f"{m.group(1)}x-access-token:{token}@{m.group(2)}"


def _git(args: Sequence[str], cwd: Path, token: Optional[str] = None,
         check: bool = True, timeout: int = 3600) -> subprocess.CompletedProcess:
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null" if os.name != "nt" else os.devnull,
    }
    if token:
        env["GIT_ASKPASS"] = "echo"
    return run([GIT, *args], cwd=str(cwd), env=env, check=check, timeout=timeout)


def clone_sparse(repo: str, workdir: Path, subdir: str, *, branch: str = "main",
                 token: Optional[str] = None, depth: int = 1) -> Path:
    """Partial + sparse clone limited to `subdir`."""
    workdir = ensure_dir(workdir)
    target = workdir / "repo"
    if target.exists():
        shutil.rmtree(target)
    url = auth_url(repo, token)
    _git(["clone", "--filter=blob:none", "--no-checkout", "--depth", str(depth),
          "--branch", branch, url, str(target)], workdir, token)
    _git(["sparse-checkout", "init", "--cone"], target, token)
    _git(["sparse-checkout", "set", subdir or "."], target, token)
    _git(["checkout", branch], target, token)
    _git(["config", "user.name", "apk2source-bot"], target, token)
    _git(["config", "user.email", "apk2source-bot@users.noreply.github.com"], target, token)
    return target


def init_repo(workdir: Path, *, branch: str = "main") -> Path:
    target = ensure_dir(workdir) / "repo"
    if target.exists():
        shutil.rmtree(target)
    ensure_dir(target)
    _git(["init", "-b", branch], target)
    _git(["config", "user.name", "apk2source-bot"], target)
    _git(["config", "user.email", "apk2source-bot@users.noreply.github.com"], target)
    return target


def enable_lfs(repo_dir: Path, patterns: Iterable[str] = LFS_PATTERNS,
               token: Optional[str] = None) -> bool:
    p = run([GIT, "lfs", "version"], cwd=str(repo_dir), check=False)
    if p.returncode != 0:
        warn("git-lfs not installed; publishing without LFS")
        return False
    _git(["lfs", "install", "--local"], repo_dir, token)
    _git(["lfs", "track", *patterns], repo_dir, token)
    attrs = repo_dir / ".gitattributes"
    log(f"LFS tracking {len(list(patterns))} patterns -> {attrs}")
    return True


def copy_tree(src: Path, dest: Path, *, only_exts: Optional[Iterable[str]] = None,
              max_file_bytes: int = 0) -> Dict[str, Any]:
    dest = ensure_dir(dest)
    exts = {e.lower() for e in only_exts} if only_exts else None
    copied = skipped = 0
    total = 0
    for p in Path(src).rglob("*"):
        if not p.is_file():
            continue
        if exts and p.suffix.lower() not in exts:
            skipped += 1
            continue
        sz = p.stat().st_size
        if max_file_bytes and sz > max_file_bytes:
            skipped += 1
            continue
        rel = p.relative_to(src)
        out = dest / rel
        ensure_dir(out.parent)
        shutil.copy2(p, out)
        copied += 1
        total += sz
    return {"copied": copied, "skipped": skipped, "bytes": total}


def commit_and_push(repo_dir: Path, *, remote_url: str, branch: str, message: str,
                    token: Optional[str] = None, retries: int = 4,
                    force: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    rec: Dict[str, Any] = {"branch": branch, "message": message}
    _git(["add", "-A"], repo_dir, token)
    status = _git(["status", "--porcelain"], repo_dir, token).stdout or ""
    rec["changed_files"] = len([ln for ln in status.splitlines() if ln.strip()])
    if not rec["changed_files"]:
        rec["pushed"] = False
        rec["reason"] = "no changes"
        log("nothing to commit")
        return rec
    if dry_run:
        rec["pushed"] = False
        rec["dry_run"] = True
        rec["sample"] = status.splitlines()[:40]
        return rec

    _git(["commit", "-m", message], repo_dir, token)
    if not remote_url:
        rec["pushed"] = False
        rec["reason"] = "no remote"
        return rec

    _git(["remote", "remove", "origin"], repo_dir, token, check=False)
    _git(["remote", "add", "origin", remote_url], repo_dir, token)

    last_err = ""
    for attempt in range(1, retries + 1):
        args = ["push", "origin", f"HEAD:{branch}"]
        if force:
            args.insert(1, "--force")
        p = _git(args, repo_dir, token, check=False, timeout=7200)
        if p.returncode == 0:
            rec["pushed"] = True
            rec["attempts"] = attempt
            log(f"pushed {rec['changed_files']} files to {branch}")
            return rec
        last_err = ((p.stderr or "") + (p.stdout or ""))[-2000:]
        warn(f"push attempt {attempt} failed: {last_err[-300:]}")
        time.sleep(10 * attempt)
    rec["pushed"] = False
    rec["error"] = last_err
    return rec


def open_pr(repo: str, head: str, base: str, title: str, body: str,
            token: str) -> Optional[str]:
    import urllib.request

    url = f"https://api.github.com/repos/{repo}/pulls"
    data = json.dumps({"title": title, "head": head, "base": base, "body": body}).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "apk2source",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
            return json.loads(r.read()).get("html_url")
    except Exception as e:  # noqa: BLE001
        warn(f"open_pr failed: {e}")
        return None


# --------------------------------------------------------------------------
# high level
# --------------------------------------------------------------------------
def publish(src: Path, *, repo: str, subdir: str, token: Optional[str],
            branch: str = "main", message: str = "apk2source: update",
            workdir: Optional[Path] = None, use_lfs: bool = True,
            lfs_threshold: int = DEFAULT_LFS_THRESHOLD,
            only_exts: Optional[Iterable[str]] = None,
            mode: str = "direct", dry_run: bool = False,
            base_branch: str = "main") -> Dict[str, Any]:
    """Copy `src` into `<repo>/<subdir>` and push."""
    src = Path(src)
    workdir = Path(workdir or ensure_dir(src.parent / "_publish"))
    ensure_dir(workdir)
    rec: Dict[str, Any] = {"stage": "publish", "repo": repo, "subdir": subdir,
                           "branch": branch, "mode": mode, "src": str(src)}

    largest = 0
    for p in src.rglob("*"):
        if p.is_file():
            largest = max(largest, p.stat().st_size)
    rec["largest_file"] = largest
    lfs = use_lfs and largest >= lfs_threshold

    try:
        repo_dir = clone_sparse(repo, workdir, subdir, branch=base_branch, token=token)
        rec["clone"] = "sparse"
    except Exception as e:  # noqa: BLE001
        warn(f"sparse clone failed ({e}); initialising a fresh repo")
        repo_dir = init_repo(workdir, branch=branch)
        rec["clone"] = "fresh"

    dest = repo_dir / subdir if subdir else repo_dir
    ensure_dir(dest)
    rec["copy"] = copy_tree(src, dest, only_exts=only_exts)
    if not rec["copy"]["copied"]:
        rec["pushed"] = False
        rec["reason"] = "nothing to copy"
        write_json(workdir / "publish.json", rec)
        return rec

    if lfs:
        rec["lfs"] = enable_lfs(repo_dir, token=token)
        gitattributes = repo_dir / ".gitattributes"
        if gitattributes.exists() and dest != repo_dir:
            shutil.copy2(gitattributes, dest / ".gitattributes")

    push_branch = branch
    if mode == "pr":
        push_branch = f"apk2source/{slug(subdir or 'update')}-{int(time.time())}"
    rec["push_branch"] = push_branch
    rec.update(commit_and_push(repo_dir, remote_url=auth_url(repo, token),
                               branch=push_branch, message=message,
                               token=token, dry_run=dry_run))
    if mode == "pr" and rec.get("pushed") and token:
        rec["pr"] = open_pr(repo, push_branch, base_branch,
                            f"apk2source: {subdir or repo}", message, token)
    write_json(workdir / "publish.json", rec)
    log(f"publish -> {repo}/{subdir}: copied={rec['copy']['copied']} "
        f"({human(rec['copy']['bytes'])}) pushed={rec.get('pushed')}")
    return rec
