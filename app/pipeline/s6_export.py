"""Stage 6 — Export。

WAV 48kHz/24bit(保管用)/ MP3 192kbps CBR / AAC 128kbps + メタデータ + QC レポート。
書き出しは「永続」扱いで GC 対象にしない(§6.2)。
"""
from __future__ import annotations

import json

from . import ffmpeg
from .base import StageContext

FORMATS = {
    "wav": (["-c:a", "pcm_s24le"], "episode.wav"),
    "mp3": (["-c:a", "libmp3lame", "-b:a", "192k"], "episode.mp3"),
    "aac": (["-c:a", "aac", "-b:a", "128k"], "episode.m4a"),
}


def run(ctx: StageContext, params: dict) -> dict:
    src = ctx.upstream_dir("master") / "master.flac"
    meta = []
    for key in ("title", "artist", "album"):
        if params.get(key):
            meta += ["-metadata", f"{key}={params[key]}"]

    artifacts = {}
    for fmt, (codec, filename) in FORMATS.items():
        ctx.progress(f"export: {fmt}")
        out = ctx.out_dir / filename
        ffmpeg.run(["-i", str(src), *codec, *meta, str(out)])
        artifacts[fmt] = {"file": filename, "bytes": out.stat().st_size}

    # QC レポート: 各ステージの測定値を1枚に集約
    ctx.progress("export: QC レポート")
    stages = ctx.doc["stages"]
    qc = {
        "sync": stages["sync"]["report"],
        "master": stages["master"]["report"],
        "warnings": [
            f"{s}: {w}"
            for s in ctx.doc["stage_order"]
            for w in stages[s].get("report", {}).get("warnings", [])
        ],
    }
    (ctx.out_dir / "qc_report.json").write_text(
        json.dumps(qc, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    artifacts["qc_report"] = {"file": "qc_report.json"}
    return {"artifacts": artifacts, "warnings": qc["warnings"]}
