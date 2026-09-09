from __future__ import annotations

import json
from pathlib import Path

from apk2source import report
from apk2source.util import slug


def test_slug():
    assert slug("Return to Monkey Island!") == "Return-to-Monkey-Island"
    assert slug("../../etc") == "etc"
    assert slug("") == "unnamed"
    assert slug("a" * 200) == "a" * 80


def test_report_roundtrip(tmp_path: Path):
    work = tmp_path / "work"
    report.save_stage(work, "acquire", {"ok": True, "size": 1234, "size_human": "1.2KB",
                                        "sha256": "ab" * 32, "cached": False})
    report.save_stage(work, "engine_detect", {"ok": True, "engine": "unity",
                                              "unity_version": "2021.3.16f1"})
    report.save_stage(work, "spine_extract", {
        "ok": True,
        "stats": {"skeletons": 3, "complete": 3, "textures": 3, "bytes": 999},
        "skeletons": [{"name": "hero", "skeleton_format": "json",
                       "animations": ["idle"], "complete": True, "files": [{}]}],
    })

    rep = report.build_report(work, game="demo", run_id="42")
    assert rep["summary"]["engine"] == "unity"
    assert rep["summary"]["skeletons"] == 3
    assert (work / "pipeline.json").exists()

    md = report.markdown(rep)
    assert "apk2source - demo" in md
    assert "| `unity` |" in md
    assert "hero" in md

    stages = {s["key"]: s["status"] for s in rep["stages"]}
    assert stages["acquire"] == "ok"
    assert stages["device_cache"] == "missing"


def test_load_stage_missing(tmp_path: Path):
    assert report.load_stage(tmp_path / "nope", "acquire") is None
