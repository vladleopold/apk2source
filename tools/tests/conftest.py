"""Shared pytest fixtures: builds the synthetic Unity/Spine payload once."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(Path(__file__).parent.parent))

import make_fixture  # noqa: E402


@pytest.fixture(scope="session")
def unity_fixture(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("fixture")
    man = make_fixture.build_all(str(out), "2021.3.16f1")
    man["_dir"] = str(out)
    return man


@pytest.fixture(scope="session")
def fake_apks(unity_fixture, tmp_path_factory) -> Path:
    """A synthetic .apks split set carrying the fixture."""
    d = tmp_path_factory.mktemp("apks")
    fx = Path(unity_fixture["_dir"])

    base = d / "base.apk"
    with zipfile.ZipFile(base, "w", zipfile.ZIP_DEFLATED) as z:
        from apk2source.cli import _fake_axml

        z.writestr("AndroidManifest.xml", _fake_axml("com.example.synthetic"))
        z.writestr("assets/bin/Data/sharedassets0.assets", (fx / "sharedassets0.assets").read_bytes())
        z.writestr("classes.dex", b"dex\n035\x00" + b"\x00" * 32)

    pack = d / "split_ggpack1.apk"
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("assets/aa/Android/spine_assets.bundle", (fx / "spine_assets.bundle").read_bytes())

    apks = d / "game.apks"
    with zipfile.ZipFile(apks, "w", zipfile.ZIP_STORED) as z:
        z.write(base, "base.apk")
        z.write(pack, "split_ggpack1.apk")
    return apks
