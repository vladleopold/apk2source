from __future__ import annotations

import json
from pathlib import Path

import pytest

from apk2source import spine


ATLAS = """page0.png
size: 64,128
format: RGBA8888
filter: Linear,Linear
repeat: none
head
  rotate: false
  xy: 2, 2
  size: 32, 32
  orig: 32, 32
  offset: 0, 0
  index: -1
"""

SKELETON = json.dumps({
    "skeleton": {"hash": "abc", "spine": "3.8.75", "width": 100, "height": 200, "fps": 30},
    "bones": [{"name": "root"}],
    "slots": [{"name": "head", "bone": "root"}],
    "skins": [],
    "animations": {"idle": {}},
}).encode()


def test_detect_atlas_positive():
    info = spine.detect_atlas(ATLAS.encode())
    assert info and info["kind"] == "atlas"
    assert info["pages"][0]["width"] == 64
    assert info["regions"] == 1


def test_detect_atlas_rejects_plain_text():
    assert spine.detect_atlas(b"hello world\nsize: 1,2\n") is None
    assert spine.detect_atlas(b"") is None
    assert spine.detect_atlas(b"\x00\x01\x02binary") is None


def test_detect_atlas_rejects_json():
    assert spine.detect_atlas(SKELETON) is None


def test_detect_skeleton_json():
    info = spine.detect_skeleton_json(SKELETON)
    assert info and info["spine_version"] == "3.8.75"
    assert info["animations"] == ["idle"]


def test_detect_skeleton_json_rejects_generic_json():
    assert spine.detect_skeleton_json(b'{"foo": 1}') is None
    assert spine.detect_skeleton_json(b"not json") is None


def test_detect_skeleton_binary_spine_header():
    import sys

    sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
    import make_fixture

    blob = make_fixture.make_skel_binary("hero")
    info = spine.detect_skeleton_binary(blob)
    assert info and info["kind"] == "skel"
    assert info["spine_version"] == "4.1.15"
    assert info["width"] == pytest.approx(241.0, abs=0.01)


def test_detect_skeleton_binary_rejects_noise():
    assert spine.detect_skeleton_binary(b"\x00" * 64) is None
    assert spine.detect_skeleton_binary(b"PK\x03\x04 not a skeleton at all") is None


def test_scan_and_group_roundtrip(tmp_path: Path):
    root = tmp_path / "assets"
    d = root / "bundle_a" / "spine"
    d.mkdir(parents=True)
    (d / "hero.json").write_bytes(SKELETON)
    (d / "hero.atlas").write_text(ATLAS.replace("page0.png", "hero.png"))
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    (d / "hero.png").write_bytes(png)

    cands = spine.scan_tree(root)
    kinds = sorted(c.kind for c in cands)
    assert kinds == ["atlas", "image", "json"]

    skeletons = spine.group(cands)
    assert len(skeletons) == 1
    s = skeletons[0]
    assert s.name == "hero"
    assert s.json is not None and s.atlases and s.textures
    assert s.complete


def test_export_layout(tmp_path: Path):
    root = tmp_path / "assets" / "b"
    root.mkdir(parents=True)
    (root / "hero.json").write_bytes(SKELETON)
    (root / "hero.atlas").write_text(ATLAS.replace("page0.png", "hero.png"))
    (root / "hero.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    out = tmp_path / "spine"
    rec = spine.run(tmp_path / "assets", out, "My Game!")
    assert rec["stats"]["skeletons"] == 1
    from apk2source.util import slug as _slug

    game_dir = out / _slug("My Game!")
    assert (game_dir / "_index.json").exists()
    assert (game_dir / "hero" / "hero.json").exists()
    assert (game_dir / "hero" / "hero.atlas").exists()
    assert (game_dir / "hero" / "_meta.json").exists()


def test_content_dedupe(tmp_path: Path):
    root = tmp_path / "assets"
    for bundle in ("b1", "b2", "b3"):
        d = root / bundle
        d.mkdir(parents=True)
        (d / "hero.json").write_bytes(SKELETON)
    cands = spine.scan_tree(root)
    assert len([c for c in cands if c.kind == "json"]) == 1
