from __future__ import annotations

import json
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest

from apk2source import containers, device


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


def test_manifest_package_prefers_root_attribute(tmp_path: Path):
    def binary_manifest(package: str) -> bytes:
        strings = ["manifest", "package", package, "com.android.example"]
        encoded = bytearray()
        offsets = []
        for value in strings:
            offsets.append(len(encoded))
            raw = value.encode("utf-8")
            encoded.append(len(raw))
            encoded.append(len(raw))
            encoded.extend(raw)
            encoded.append(0)
        encoded.append(0)
        header_len = 28 + 4 * len(strings)
        pool_size = header_len + len(encoded)
        pool = struct.pack("<HHI", 0x0001, 28, pool_size)
        pool += struct.pack("<IIIII", len(strings), 0, 1 << 8, header_len, 0)
        pool += b"".join(struct.pack("<I", offset) for offset in offsets)
        pool += bytes(encoded)
        root = struct.pack("<HHI", 0x0102, 16, 56)
        root += struct.pack("<IIII", 1, 0xFFFFFFFF, 0xFFFFFFFF, 0)
        root += struct.pack("<HHHHHH", 20, 20, 1, 0, 0, 0)
        root += struct.pack("<III", 0xFFFFFFFF, 1, 2)
        root += struct.pack("<HBBI", 8, 0, 0x03, 2)
        document = b"\x03\x00\x08\x00" + struct.pack("<I", 8 + pool_size + len(root))
        document += pool + root
        return document

    apk = tmp_path / "base.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", binary_manifest("com.devolverdigital.com.rtmi"))
    assert containers.parse_manifest(apk)["package"] == "com.devolverdigital.com.rtmi"


def test_merged_listing(tmp_path: Path, fake_apks: Path):
    rec = containers.unpack(fake_apks, tmp_path / "out")
    listing = json.loads((Path(rec["merged"]).parent / "meta" / "merged_listing.json").read_text())
    assert listing["total_bytes"] > 0
    assert "assets" in listing["by_top_level"]


def test_resolve_package_validates_hint(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def adb_out(*args, **kwargs):
        calls.append(args)
        return "package:com.devolverdigital.com.rtmi\npackage:com.other.app\n"

    monkeypatch.setattr(device, "adb_out", adb_out)

    assert device.resolve_package("emulator-5554", "com.devolverdigital.com.rtmi") == "com.devolverdigital.com.rtmi"
    assert device.resolve_package("emulator-5554", "com.invalid") == "com.other.app"
    assert calls == [
        ("shell", "pm", "list", "packages", "-3"),
        ("shell", "pm", "list", "packages", "-3"),
    ]


def test_install_filters_abi_splits_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in (
        "base.apk",
        "split_config.arm64-v8a.apk",
        "split_config.x86_64.apk",
        "split_ggpack1.apk",
    ):
        (tmp_path / name).write_bytes(b"apk")

    def device_info(serial):
        return {"abi": "arm64-v8a"}

    calls = []

    def adb(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="Success", stderr="")

    monkeypatch.setattr(device, "device_info", device_info)
    monkeypatch.setattr(device, "adb", adb)

    result = device.install(tmp_path, "com.devolverdigital.com.rtmi", "emulator-5554")

    assert result["ok"]
    assert result["installed"] == [
        "base.apk",
        "split_config.arm64-v8a.apk",
        "split_ggpack1.apk",
    ]
    assert calls[0][0][0] == "install-multiple"
    assert "split_config.x86_64.apk" not in calls[0][0]
