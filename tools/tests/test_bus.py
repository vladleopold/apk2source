from __future__ import annotations

import os
from pathlib import Path

import pytest

from apk2source import bus


def test_local_bus_roundtrip(tmp_path: Path):
    root = tmp_path / "bus"
    b = bus.LocalBus(root)
    src = tmp_path / "payload.bin"
    src.write_bytes(os.urandom(4096))

    rec = b.put(src, "game/acquire/payload.bin")
    assert rec["stored"] and rec["bytes"] == 4096
    assert b.exists("game/acquire/payload.bin")

    dest = tmp_path / "out" / "payload.bin"
    b.get("game/acquire/payload.bin", dest)
    assert dest.read_bytes() == src.read_bytes()

    listing = b.list("game/")
    assert any(e["key"].endswith("payload.bin") for e in listing)

    assert b.delete("game/acquire/payload.bin")
    assert not b.exists("game/acquire/payload.bin")


def test_local_bus_rejects_escape(tmp_path: Path):
    b = bus.LocalBus(tmp_path / "bus")
    with pytest.raises(bus.BusError):
        b.put(tmp_path, "../../evil.bin")


def test_null_bus(tmp_path: Path):
    b = bus.NullBus()
    assert b.put(tmp_path, "k") == {"stored": False}
    assert b.exists("k") is False
    assert b.list() == []
    with pytest.raises(bus.BusError):
        b.get("k", tmp_path / "x")


def test_from_env_defaults_to_none(monkeypatch):
    monkeypatch.delenv("APK2SOURCE_BUS", raising=False)
    assert isinstance(bus.from_env(), bus.NullBus)
    monkeypatch.setenv("APK2SOURCE_BUS", "local")
    monkeypatch.setenv("APK2SOURCE_BUS_LOCAL", "/tmp/apk2source-bus-test")
    assert isinstance(bus.from_env(), bus.LocalBus)


def test_from_env_s3_requires_full_config(monkeypatch):
    monkeypatch.setenv("APK2SOURCE_BUS", "s3")
    monkeypatch.delenv("APK2SOURCE_S3_ENDPOINT", raising=False)
    assert isinstance(bus.from_env(), bus.NullBus)


def test_sigv4_headers_shape():
    h = bus._sigv4_headers(
        "GET", "https://acct.r2.cloudflarestorage.com/bucket/key.bin", bus.EMPTY_SHA,
        access_key="AK", secret_key="SK", region="auto",
    )
    assert h["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/")
    assert h["x-amz-content-sha256"] == bus.EMPTY_SHA
    assert "x-amz-date" in h


def test_key_for():
    assert bus.key_for("My Game!", "acquire", "a b.apks") == "my-game/acquire/a-b.apks"
