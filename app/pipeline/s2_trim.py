"""Stage 2 — Trim(収録前後の手動トリム)。

Sync が作った共通時間軸から、アップロードされた話者と reference に同じ範囲を
サンプル単位で適用する。自動無音検出やトラック別の範囲指定は行わない。
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffmpeg
from .base import SPEAKERS, StageContext

SR = 48000


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} は数値で指定してください")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} は有限な数値で指定してください")
    return value


def resolve_bounds(
    params: dict, program_length_samples: int, sr: int = SR
) -> dict[str, float | int]:
    """Trim の指定を検証し、48kHzサンプル境界へ丸めて返す。

    ``end_s=None`` は Sync の全長を意味する。丸め後にも境界を検証するため、
    極端に小さい範囲がゼロサンプルになった場合も実行前に拒否できる。
    """
    if isinstance(program_length_samples, bool) or not isinstance(program_length_samples, int):
        raise ValueError("Sync の program_length_samples が正しくありません")
    if program_length_samples <= 0:
        raise ValueError("Sync の program_length_samples が正しくありません")
    program_length_s = program_length_samples / sr
    start_s = _number(params.get("start_s", 0.0), "start_s")
    raw_end = params.get("end_s")
    end_s = program_length_s if raw_end is None else _number(raw_end, "end_s")
    if not (0 <= start_s < end_s <= program_length_s):
        raise ValueError(
            f"Trim 範囲が不正です: 0 <= start_s < end_s <= {program_length_s:.6f} を満たしてください"
        )

    # JS の Math.round と同じ、正の値の half-up に固定する。
    start_sample = math.floor(start_s * sr + 0.5)
    end_sample = math.floor(end_s * sr + 0.5)
    if not (0 <= start_sample < end_sample <= program_length_samples):
        raise ValueError("Trim 範囲が48kHzサンプル単位で空になるか、範囲外です")
    applied_start = start_sample / sr
    applied_end = end_sample / sr
    return {
        "requested_start_s": start_s,
        "requested_end_s": None if raw_end is None else end_s,
        "start_sample": start_sample,
        "end_sample": end_sample,
        "start_s": applied_start,
        "end_s": applied_end,
        "output_length_s": (end_sample - start_sample) / sr,
        "program_length_s": program_length_s,
        "program_length_samples": program_length_samples,
    }


def _input_paths(ctx: StageContext) -> dict[str, Path]:
    return {
        **{r: ctx.upstream_dir("sync") / f"{r}.flac" for r in SPEAKERS},
        "reference": ctx.upstream_dir("sync") / "reference.flac",
    }


def trim_filter(start_sample: int, end_sample: int) -> str:
    """指定サンプル範囲を新しい t=0 へ移すFFmpegフィルタ列。

    入力はSyncで共通長へ揃え済みなので、無音を補わず指定範囲だけを切る。
    """
    return (
        f"atrim=start_sample={start_sample}:end_sample={end_sample},"
        "asetpts=PTS-STARTPTS"
    )


def _trim_one(ctx: StageContext, role: str, src: Path, start_sample: int, end_sample: int) -> dict:
    ctx.progress(f"trim 適用: {role}")
    out = ctx.out_dir / f"{role}.flac"
    # atrim の end_sample は排他的。asetpts で切り出し後の t=0 を新しい原点にする。
    chain = trim_filter(start_sample, end_sample)
    ffmpeg.run(["-i", str(src), "-af", chain, *ffmpeg.FLAC24, str(out)])
    info = ffmpeg.probe(out)
    ctx.make_preview(out, role)
    ctx.make_peaks(out, role)
    return {"duration_s": info["duration"], "sample_rate": info["sample_rate"]}


def run(ctx: StageContext, params: dict) -> dict:
    sync_report = ctx.doc["stages"]["sync"].get("report", {})
    bounds = resolve_bounds(params, sync_report.get("program_length_samples", 0))
    start_sample = int(bounds["start_sample"])
    end_sample = int(bounds["end_sample"])

    sources = _input_paths(ctx)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            role: pool.submit(
                _trim_one, ctx, role, sources[role], start_sample, end_sample
            )
            for role in sources
        }
        tracks = {role: future.result() for role, future in futures.items()}

    expected = float(bounds["output_length_s"])
    lengths = {role: info["duration_s"] for role, info in tracks.items()}
    # ffprobe の丸め誤差は許容するが、全トラックに同じ範囲を適用できたことを確認する。
    if any(abs(length - expected) > 2 / SR for length in lengths.values()):
        raise ValueError("Trim 出力の長さが指定範囲と一致しません")
    if max(lengths.values()) - min(lengths.values()) > 2 / SR:
        raise ValueError("Trim の全トラックの出力長が一致しません")

    report = {
        "sample_rate": SR,
        "requested_start_s": bounds["requested_start_s"],
        "requested_end_s": bounds["requested_end_s"],
        "applied_start_s": bounds["start_s"],
        "applied_end_s": bounds["end_s"],
        "start_s": bounds["start_s"],
        "end_s": bounds["end_s"],
        "start_sample": start_sample,
        "end_sample": end_sample,
        "output_length_s": expected,
        "program_length_s": bounds["program_length_s"],
        "program_length_samples": bounds["program_length_samples"],
        "tracks": tracks,
        "warnings": [],
    }
    report["requested"] = {
        "start_s": report["requested_start_s"],
        "end_s": report["requested_end_s"],
    }
    report["applied"] = {
        "start_s": report["applied_start_s"],
        "end_s": report["applied_end_s"],
    }
    return report
