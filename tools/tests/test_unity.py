from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from apk2source import engine, spine, unity_extract


def test_find_unity_files(tmp_path: Path, unity_fixture):
    fx = Path(unity_fixture["_dir"])
    root = tmp_path / "merged"
    (root / "assets/bin/Data").mkdir(parents=True)
    (root / "assets/bin/Data/sharedassets0.assets").write_bytes((fx / "sharedassets0.assets").read_bytes())
    (root / "assets/aa/Android").mkdir(parents=True)
    (root / "assets/aa/Android/spine.bundle").write_bytes((fx / "spine_assets.bundle").read_bytes())
    (root / "classes.dex").write_bytes(b"dex\n035\x00")

    found = unity_extract.find_unity_files(root)
    names = {p.name for p in found}
    assert "sharedassets0.assets" in names
    assert "spine.bundle" in names
    assert "classes.dex" not in names


def test_engine_detection(tmp_path: Path, unity_fixture):
    fx = Path(unity_fixture["_dir"])
    root = tmp_path / "merged"
    (root / "assets/bin/Data").mkdir(parents=True)
    (root / "assets/bin/Data/sharedassets0.assets").write_bytes((fx / "sharedassets0.assets").read_bytes())
    (root / "lib/arm64-v8a").mkdir(parents=True)
    (root / "lib/arm64-v8a/libil2cpp.so").write_bytes(b"\x7fELF" + b"\x00" * 32)
    (root / "lib/arm64-v8a/libunity.so").write_bytes(b"\x7fELF" + b"\x00" * 32)

    rep = engine.detect(root)
    assert rep["engine"] == "unity"
    assert rep["unity_version"] == "2021.3.16f1"
    assert "libil2cpp.so" in rep["native_libs"]
    assert rep["abis"] == ["arm64-v8a"]


def test_extract_all_and_spine(tmp_path: Path, unity_fixture):
    fx = Path(unity_fixture["_dir"])
    root = tmp_path / "merged"
    (root / "assets/bin/Data").mkdir(parents=True)
    (root / "assets/bin/Data/sharedassets0.assets").write_bytes((fx / "sharedassets0.assets").read_bytes())

    assets = tmp_path / "assets_out"
    uni = unity_extract.extract_all(root, assets, jobs=1)
    assert uni["files_ok"] == 1
    assert uni["totals"]["TextAsset"] == 9
    assert uni["totals"]["Texture2D"] == 3
    assert uni["unity_versions"] == ["2021.3.16f1"]

    rec = spine.run(assets, tmp_path / "spine", "testgame")
    assert rec["stats"]["skeletons"] == 3
    assert rec["stats"]["complete"] == 3

    expect = unity_fixture["expect"]
    from apk2source.util import slug as _slug

    for name, exp in expect.items():
        d = Path(rec["out"]) / _slug(name)
        meta = json.loads((d / "_meta.json").read_text())
        by_role = {f["role"]: f for f in meta["files"]}
        assert by_role["skeleton_json"]["sha256"] == exp["json_sha256"]
        assert by_role["atlas"]["sha256"] == exp["atlas_sha256"]
        assert by_role["skeleton_binary"]["sha256"] == exp["skel_sha256"]

        Image = pytest.importorskip("PIL.Image")
        img = Image.open(d / by_role["texture"]["name"]).convert("RGBA")
        assert img.size == tuple(exp["png_size"])
        assert hashlib.sha256(img.tobytes()).hexdigest() == exp["rgba_sha256_flipped"]


def test_container_paths_preserved(tmp_path: Path, unity_fixture):
    """AssetBundle m_Container paths must survive as directories (spine pairing)."""
    fx = Path(unity_fixture["_dir"])
    assets = tmp_path / "assets_out"
    unity_extract.extract_all(fx, assets, jobs=1)
    hits = list(assets.rglob("spine/*.json")) + list(assets.rglob("spine/*.atlas"))
    assert hits, f"container path 'assets/spine/...' not preserved: {list(assets.rglob('*'))[:20]}"
