"""Pipeline manifest + report generation.

Every stage writes `<work>/state/<stage>.json`. This module folds them into a
single `pipeline.json` that the GitHub Pages control panel renders, plus a
Markdown summary for `$GITHUB_STEP_SUMMARY`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .util import ensure_dir, human, log, read_json, write_json

STAGES = [
    ("acquire", "01-acquire", "Download .apk/.apks (cached or fresh)"),
    ("unpack", "02-unpack", "Normalise container -> merged APK tree"),
    ("engine_detect", "02b-engine", "Fingerprint engine + Spine signals"),
    ("device_cache", "03-device-cache", "Install, first-run, capture runtime cache"),
    ("unity_extract", "04-decompile", "Unity binaries -> asset tree"),
    ("spine_extract", "05-spine", "Asset tree -> json / atlas / png"),
    ("publish", "06-publish", "Push to source_spine / game_source"),
]


def state_dir(work: Path) -> Path:
    return ensure_dir(Path(work) / "state")


def save_stage(work: Path, stage: str, record: Dict[str, Any]) -> Path:
    p = state_dir(work) / f"{stage}.json"
    record = {"stage": stage, "recorded_at": time.time(), **record}
    write_json(p, record)
    return p


def load_stage(work: Path, stage: str) -> Optional[Dict[str, Any]]:
    return read_json(state_dir(work) / f"{stage}.json")


def build_report(work: Path, *, game: str = "", run_id: str = "",
                 extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    work = Path(work)
    stages: List[Dict[str, Any]] = []
    for key, wf, desc in STAGES:
        rec = load_stage(work, key)
        stages.append({
            "key": key,
            "workflow": wf,
            "description": desc,
            "status": "missing" if rec is None else ("ok" if rec.get("ok", True) else "failed"),
            "record": rec,
        })

    spine = load_stage(work, "spine_extract") or {}
    unity = load_stage(work, "unity_extract") or {}
    acq = load_stage(work, "acquire") or {}
    eng = load_stage(work, "engine_detect") or {}
    dev = load_stage(work, "device_cache") or {}
    pub = load_stage(work, "publish") or {}

    report = {
        "schema": 1,
        "game": game,
        "run_id": run_id or os.environ.get("GITHUB_RUN_ID", ""),
        "generated_at": time.time(),
        "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "actor": os.environ.get("GITHUB_ACTOR", ""),
        "summary": {
            "engine": eng.get("engine"),
            "unity_version": eng.get("unity_version") or (unity.get("unity_versions") or [None])[0],
            "payload_size": acq.get("size"),
            "payload_size_human": acq.get("size_human"),
            "payload_sha256": acq.get("sha256"),
            "cached": acq.get("cached"),
            "package": (load_stage(work, "unpack") or {}).get("package"),
            "skeletons": (spine.get("stats") or {}).get("skeletons", 0),
            "skeletons_complete": (spine.get("stats") or {}).get("complete", 0),
            "textures": (spine.get("stats") or {}).get("textures", 0),
            "spine_bytes": (spine.get("stats") or {}).get("bytes", 0),
            "unity_files": unity.get("files_total", 0),
            "unity_files_ok": unity.get("files_ok", 0),
            "device_cache_files": dev.get("cache_files", 0),
            "device_cache_bytes": dev.get("cache_bytes", 0),
            "published": pub.get("pushed", False),
        },
        "stages": stages,
        **(extra or {}),
    }
    write_json(work / "pipeline.json", report)
    return report


def markdown(report: Dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"## apk2source - {report.get('game') or '(unnamed)'}",
        "",
        "| field | value |",
        "|---|---|",
        f"| engine | `{s.get('engine')}` |",
        f"| unity version | `{s.get('unity_version')}` |",
        f"| package | `{s.get('package')}` |",
        f"| payload | {s.get('payload_size_human')} (`{(s.get('payload_sha256') or '')[:16]}...`) |",
        f"| cache hit | {s.get('cached')} |",
        f"| unity files | {s.get('unity_files_ok')}/{s.get('unity_files')} ok |",
        f"| device cache | {s.get('device_cache_files')} files / {human(s.get('device_cache_bytes') or 0)} |",
        f"| **spine skeletons** | **{s.get('skeletons')}** ({s.get('skeletons_complete')} complete) |",
        f"| spine textures | {s.get('textures')} ({human(s.get('spine_bytes') or 0)}) |",
        f"| published | {s.get('published')} |",
        "",
        "### Stages",
        "",
        "| stage | status |",
        "|---|---|",
    ]
    for st in report["stages"]:
        icon = {"ok": "pass", "failed": "FAIL", "missing": "skip"}[st["status"]]
        lines.append(f"| {st['workflow']} | {icon} |")
    spine_recs = (report["stages"][-2]["record"] or {}).get("skeletons") or []
    if spine_recs:
        lines += ["", "### Skeletons", "", "| name | format | animations | complete | files |", "|---|---|---|---|---|"]
        for e in spine_recs[:60]:
            lines.append(
                f"| `{e['name']}` | {e.get('skeleton_format')} | {len(e.get('animations') or [])} "
                f"| {'yes' if e.get('complete') else 'no'} | {len(e.get('files') or [])} |"
            )
    return "\n".join(lines)


def emit(work: Path, game: str, run_id: str = "") -> Dict[str, Any]:
    report = build_report(Path(work), game=game, run_id=run_id)
    md = markdown(report)
    from .util import gh_summary

    gh_summary(md)
    log("report written to " + str(Path(work) / "pipeline.json"))
    return report
