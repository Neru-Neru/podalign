"""Stage 0 — Ingest(取り込み・検証)。

作業フォーマットへ統一: 話者 48kHz/モノ/FLAC24。BGM・ジングルはステレオ維持。
reference は同期解析とTrim確認用なのでモノ化して保持する。
QC: クリッピング率 / DC オフセット / 無音率 / 長さの不一致。
"""
from __future__ import annotations

import numpy as np

from . import analysis, ffmpeg
from .base import StageContext


def run(ctx: StageContext, params: dict) -> dict:
    report: dict = {"tracks": {}, "warnings": []}
    durations: dict[str, float] = {}

    for role in [*ctx.speakers, "reference", "jingle", "bgm"]:
        src = ctx.asset(role)
        ctx.progress(f"ingest: {role}")
        info = ffmpeg.probe(src)
        mono = role in (*ctx.speakers, "reference")
        out = ctx.out_dir / f"{role}.flac"
        args = ["-i", str(src), "-ar", "48000"]
        # 話者/reference はモノ、jingle/bgm は 2ch に固定
        # (モノ素材が来ても Stage 4 の amix でチャンネル数を揃えるため)
        args += ["-ac", "1"] if mono else ["-ac", "2"]
        ffmpeg.run(args + [*ffmpeg.FLAC24, str(out)])

        stats = ffmpeg.measure_astats(out)
        x = ffmpeg.decode_f32(out, rate=8000)
        clip_ratio = float(np.mean(np.abs(x) > 0.999)) if len(x) else 0.0
        silence = analysis.silence_ratio(x, 8000)
        durations[role] = info["duration"]
        track = {
            "duration_s": round(info["duration"], 3),
            "source_codec": info["codec"],
            "source_sample_rate": info["sample_rate"],
            "source_channels": info["channels"],
            "dc_offset": stats.get("DC offset"),
            "peak_db": stats.get("Peak level dB"),
            "clip_ratio": round(clip_ratio, 6),
            "silence_ratio": round(silence, 4),
            "needs_declip": clip_ratio > 1e-5,
        }
        report["tracks"][role] = track
        if track["needs_declip"]:
            report["warnings"].append(f"{role}: クリッピング検出 ({clip_ratio:.4%})")
        if abs(track["dc_offset"] or 0) > 0.01:
            report["warnings"].append(f"{role}: DC オフセット {track['dc_offset']:.4f}")
        if role in ctx.speakers and silence > 0.9:
            report["warnings"].append(f"{role}: 無音率 {silence:.0%} — 素材の取り違え?")

        ctx.make_preview(out, role)
        ctx.make_peaks(out, role)

    # 長さの不一致チェック(話者と reference は同じ収録なので大差は異常)
    long_tracks = {r: durations[r] for r in (*ctx.speakers, "reference")}
    spread = max(long_tracks.values()) - min(long_tracks.values())
    if spread > 120:
        report["warnings"].append(
            f"話者/reference の長さ差が {spread:.0f}s あります。録音開始/停止のずれとして Sync で吸収します"
        )
    report["duration_spread_s"] = round(spread, 1)
    return report
