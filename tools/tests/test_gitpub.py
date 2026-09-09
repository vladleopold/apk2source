from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apk2source import gitpub


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                               "GIT_TERMINAL_PROMPT": "0"})


@pytest.fixture()
def bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main"], remote)

    seed = tmp_path / "seed"
    _git(["clone", str(remote), str(seed)], tmp_path)
    _git(["config", "user.name", "seed"], seed)
    _git(["config", "user.email", "seed@example.com"], seed)
    (seed / "README.md").write_text("# seed\n")
    _git(["add", "-A"], seed)
    _git(["commit", "-m", "seed"], seed)
    _git(["push", "origin", "main"], seed)
    return remote


def test_auth_url():
    assert gitpub.auth_url("o/r", None) == "https://github.com/o/r.git"
    assert "x-access-token:TOK@" in gitpub.auth_url("o/r", "TOK")
    assert gitpub.auth_url("https://github.com/o/r.git", None) == "https://github.com/o/r.git"


def test_copy_tree_filters(tmp_path: Path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "x.json").write_text("{}")
    (src / "a" / "y.bin").write_bytes(b"\x00" * 10)
    rec = gitpub.copy_tree(src, tmp_path / "dst", only_exts=(".json",))
    assert rec["copied"] == 1 and rec["skipped"] == 1
    assert (tmp_path / "dst" / "a" / "x.json").exists()


def test_publish_to_local_bare(bare_remote: Path, tmp_path: Path):
    src = tmp_path / "spine" / "game" / "hero"
    src.mkdir(parents=True)
    (src / "hero.json").write_text('{"skeleton":{}}')
    (src / "hero.atlas").write_text("hero.png\nsize: 1,1\n")

    rec = gitpub.publish(
        tmp_path / "spine" / "game",
        repo=str(bare_remote),
        subdir="game",
        token=None,
        branch="main",
        message="test publish",
        workdir=tmp_path / "pub",
        use_lfs=False,
    )
    assert rec["copy"]["copied"] == 2, rec
    assert rec["pushed"] is True, rec

    verify = tmp_path / "verify"
    _git(["clone", str(bare_remote), str(verify)], tmp_path)
    assert (verify / "game" / "hero" / "hero.json").exists()
    assert (verify / "game" / "hero" / "hero.atlas").exists()


def test_publish_no_changes_is_noop(bare_remote: Path, tmp_path: Path):
    src = tmp_path / "out"
    src.mkdir()
    (src / "a.txt").write_text("a")
    common = dict(repo=str(bare_remote), subdir="s", token=None, branch="main",
                  workdir=tmp_path / "p1", use_lfs=False)
    first = gitpub.publish(src, message="one", **common)
    assert first["pushed"] is True
    second = gitpub.publish(src, message="two", workdir=tmp_path / "p2",
                            repo=str(bare_remote), subdir="s", token=None,
                            branch="main", use_lfs=False)
    assert second["pushed"] is False
    assert second["reason"] == "no changes"


def test_publish_dry_run(bare_remote: Path, tmp_path: Path):
    src = tmp_path / "out"
    src.mkdir()
    (src / "a.txt").write_text("a")
    rec = gitpub.publish(src, repo=str(bare_remote), subdir="d", token=None,
                         branch="main", workdir=tmp_path / "p", use_lfs=False,
                         dry_run=True)
    assert rec["dry_run"] is True and rec["pushed"] is False
