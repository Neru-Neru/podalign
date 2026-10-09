"""Stage 1 — Sync(音合わせ)★ 最大の技術リスク。

reference(通話録音)を共通時間軸とし、各話者トラックの
固定オフセット + クロックドリフトを GCC-PHAT で推定して補正する。

時間の定義(unit test で往復検証しここに固定):
    off(t) = 話者時刻 − reference 時刻 = d0 + s·t   (t は reference 時刻)
    → 話者素材の τ = d0 + (1+s)·t
補正:
    1. rubberband=tempo=(1+s)  … |s| が閾値超のときのみ(§7 検証表)
    2. 補正後タイムラインでのオフセット d0' = d0/(1+s) を
       atrim=start_sample(d0'>0) / adelay=<n>S(d0'<0) でサンプル精度適用
    3. 全話者を同一長 L(補正後長の最大)へ atrim + apad=whole_len で揃える
       (reference の t=0 が共通原点 — R-4)
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import analysis, ffmpeg
from .base import StageContext

SR = 48000
MAX_PLAUSIBLE_DRIFT_PPM = 150.0


def _clamp_segment_s(segment_s: float, spk8k, ref8k) -> float:
    """短い素材でも estimate_sync の「重なり ≥ 区間長×2」を満たすよう区間長を絞る。"""
    shorter = min(len(spk8k), len(ref8k)) / analysis.ANALYSIS_SR
    return max(5.0, min(segment_s, shorter / 4))


def _estimate(ctx: StageContext, role: str, ref8k, params) -> analysis.SyncEstimate:
    ctx.progress(f"sync 推定: {role}")
    spk8k = ffmpeg.decode_f32(ctx.upstream_dir("ingest") / f"{role}.flac", rate=analysis.ANALYSIS_SR)
    est = analysis.estimate_sync(
        spk8k, ref8k,
        n_segments=int(params["n_segments"]),
        segment_s=_clamp_segment_s(float(params["segment_s"]), spk8k, ref8k),
    )
    if abs(est.drift_ppm) > MAX_PLAUSIBLE_DRIFT_PPM:
        raise ValueError(
            f"{role}: ドリフト {est.drift_ppm:.0f}ppm は民生機クロック差(±100ppm)を大きく超えます。"
            "素材の取り違えを疑ってください"
        )
    return est


def correction_filters(
    offset_s: float,
    drift_ppm: float,
    program_len_samples: int,
    drift_threshold_ppm: float,
    sr: int = SR,
) -> list[str]:
    """推定値から補正フィルタ列を組む純関数(符号は tests/test_sync.py の往復テストで固定)。"""
    s = drift_ppm * 1e-6
    filters = []
    apply_drift = abs(drift_ppm) >= drift_threshold_ppm
    if apply_drift:
        filters.append(f"rubberband=tempo={1 + s:.9f}")
    d0_corr = offset_s / (1 + s) if apply_drift else offset_s
    n = round(abs(d0_corr) * sr)
    if d0_corr >= 0:
        filters.append(f"atrim=start_sample={n}")
        filters.append("asetpts=PTS-STARTPTS")
    elif n > 0:
        filters.append(f"adelay={n}S:all=1")
    filters.append(f"atrim=end_sample={program_len_samples}")
    filters.append(f"apad=whole_len={program_len_samples}")
    return filters


def _correct(
    ctx: StageContext, role: str, est: analysis.SyncEstimate,
    program_len_samples: int, drift_threshold_ppm: float,
) -> Path:
    ctx.progress(f"sync 補正適用: {role}")
    src = ctx.upstream_dir("ingest") / f"{role}.flac"
    out = ctx.out_dir / f"{role}.flac"
    filters = correction_filters(
        est.offset_s, est.drift_ppm, program_len_samples, drift_threshold_ppm
    )
    ffmpeg.run(["-i", str(src), "-af", ",".join(filters), *ffmpeg.FLAC24, str(out)])
    return out


def _verify(ctx: StageContext, role: str, ref8k) -> dict:
    """補正後に再度相関し、残差が ±1ms 以内かを実測で確認する(§7 手順6)。"""
    ctx.progress(f"sync 検証: {role}")
    corrected = ffmpeg.decode_f32(ctx.out_dir / f"{role}.flac", rate=analysis.ANALYSIS_SR)
    est = analysis.estimate_sync(
        corrected, ref8k, n_segments=6,
        segment_s=_clamp_segment_s(30.0, corrected, ref8k),
    )
    lags = [seg["lag_ms"] for seg in est.segments if seg.get("used")]
    max_resid = max(abs(l) for l in lags) if lags else abs(est.offset_s * 1e3)
    return {
        "residual_offset_ms": round(est.offset_s * 1e3, 3),
        "residual_drift_ppm": round(est.drift_ppm, 2),
        "max_segment_residual_ms": round(max_resid, 3),
    }


def run(ctx: StageContext, params: dict) -> dict:
    ref_path = ctx.upstream_dir("ingest") / "reference.flac"
    ref8k = ffmpeg.decode_f32(ref_path, rate=analysis.ANALYSIS_SR)

    # 推定(2並列 — N-4。numpy FFT と ffmpeg サブプロセスは GIL を外れる)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {r: pool.submit(_estimate, ctx, r, ref8k, params) for r in ctx.speakers}
        estimates = {r: f.result() for r, f in futures.items()}

    # 共通タイムライン長 L = 補正後長の最大(R-4)
    def corrected_end_s(role: str) -> float:
        est = estimates[role]
        s = est.drift_ppm * 1e-6
        src_len = ffmpeg.probe(ctx.upstream_dir("ingest") / f"{role}.flac")["duration"]
        return (src_len - est.offset_s) / (1 + s)

    program_len = max(corrected_end_s(r) for r in ctx.speakers)
    # サンプル数を共通時間軸の唯一の正とする。秒へ丸めた値を後段で再び
    # サンプル化すると、既定の全範囲Trimでも末尾が欠けたり伸びたりする。
    program_samples = math.floor(program_len * SR)

    thr = float(params["drift_threshold_ppm"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(
            lambda r: _correct(ctx, r, estimates[r], program_samples, thr), ctx.speakers
        ))
        verifications = dict(zip(ctx.speakers, pool.map(lambda r: _verify(ctx, r, ref8k), ctx.speakers)))

    # Trim の編集用にreferenceも同じ原点・同じ長さの成果物へ揃える。
    # 最終ミックスには使わないが、話者とreferenceの波形を同じ座標で比較するために必要。
    reference_out = ctx.out_dir / "reference.flac"
    ffmpeg.run([
        "-i", str(ref_path), "-af",
        f"atrim=end_sample={program_samples},apad=whole_len={program_samples},"
        f"atrim=end_sample={program_samples},asetpts=PTS-STARTPTS",
        *ffmpeg.FLAC24, str(reference_out),
    ])

    warnings: list[str] = []
    for role in ctx.speakers:
        warnings += [f"{role}: {w}" for w in estimates[role].warnings]
        resid = verifications[role]["max_segment_residual_ms"]
        if abs(resid) > 1.0:
            warnings.append(
                f"{role}: 補正後残差 {resid:.2f}ms が ±1ms を超えています。"
                "reference(VoIP 録音)のジッタバッファ起因の非線形ジャンプの可能性があります"
            )

    # UI 確認用: 話者トラック重ねのチェックミックス + 各トラック preview/peaks
    check = ctx.out_dir / "_check_mix.flac"
    inputs = []
    for r in ctx.speakers:
        inputs += ["-i", str(ctx.out_dir / f"{r}.flac")]
    ffmpeg.run([*inputs, "-filter_complex",
                f"amix=inputs={len(ctx.speakers)}:normalize=0,volume=-6dB", *ffmpeg.FLAC24, str(check)])
    ctx.make_preview(check, "mix_check")
    check.unlink()
    for r in ctx.speakers:
        ctx.make_preview(ctx.out_dir / f"{r}.flac", r)
        ctx.make_peaks(ctx.out_dir / f"{r}.flac", r)
    ctx.make_preview(reference_out, "reference")
    ctx.make_peaks(reference_out, "reference")

    return {
        "offsets_ms": {r: round(estimates[r].offset_s * 1e3, 3) for r in ctx.speakers},
        "drift_ppm": {r: round(estimates[r].drift_ppm, 2) for r in ctx.speakers},
        "drift_corrected": {r: abs(estimates[r].drift_ppm) >= thr for r in ctx.speakers},
        "residual_ms": {r: verifications[r]["max_segment_residual_ms"] for r in ctx.speakers},
        "verification": verifications,
        "segments": {r: estimates[r].segments for r in ctx.speakers},
        "program_length_samples": program_samples,
        "program_length_s": program_samples / SR,
        "warnings": warnings,
    }
