from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from apk2source import containers


def test_detect_kind_apks(fake_apks: Path):
    assert containers.detect_kind(fake_apks) == "apks"


def test_detect_kind_plain_apk(tmp_path: Path):
    p = tmp_path / "plain.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00")
        z.writestr("classes.dex", b"dex\n035\x00")
    assert containers.detect_kind(p) == "apk"


def test_unpack_split_set(fake_apks: Path, tmp_path: Path):
    rec = containers.unpack(fake_apks, tmp_path / "out")
    assert rec["kind"] == "apks"
    assert len(rec["splits"]) == 2
    assert rec["package"] == "com.example.synthetic"
    merged = Path(rec["merged"])
    assert (merged / "assets/bin/Data/sharedassets0.assets").exists()
    assert (merged / "assets/aa/Android/spine_assets.bundle").exists()
    assert rec["asset_packs"], "ggpack split should be classified as an asset pack"


def test_zip_slip_guard(tmp_path: Path):
    from apk2source.util import safe_join

    with pytest.raises(ValueError):
        safe_join(tmp_path, "../../etc/passwd")


def test_axml_string_pool():
    from apk2source.cli import _fake_axml

    strings = containers.parse_axml_strings(_fake_axml("com.a.b"))
    assert "com.a.b" in strings


def test_merged_listing(tmp_path: Path, fake_apks: Path):
    rec = containers.unpack(fake_apks, tmp_path / "out")
    listing = json.loads((Path(rec["merged"]).parent / "meta" / "merged_listing.json").read_text())
    assert listing["total_bytes"] > 0
    assert "assets" in listing["by_top_level"]
