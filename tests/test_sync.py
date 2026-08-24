"""Sync のユニットテスト(設計 §12.1 — 最重要)。

既知のオフセット+ドリフトを人工的に与えた合成トラックから推定値が復元
できること、および ffmpeg(rubberband)での補正往復で残差が消えることを検証。
ドリフトは numpy の線形補間で与える(補正に使う rubberband と独立な手段で
生成することで、符号・倍率の取り違えを検出できる)。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import analysis
from app.pipeline.s1_sync import correction_filters

SR = 8000
DUR = 600.0


def speechlike(seed: int, dur: float = DUR, sr: int = SR) -> np.ndarray:
    """発話様の信号: ランダムなオンオフ包絡 × 帯域ノイズ。"""
    rng = np.random.default_rng(seed)
    n = int(dur * sr)
    noise = rng.standard_normal(n).astype(np.float64)
    kernel = np.hanning(16)
    noise = np.convolve(noise, kernel / kernel.sum(), mode="same")
    # 平均2秒オン/1.5秒オフの矩形をなまらせた包絡
    env = np.zeros(n)
    t = 0
    on = True
    while t < n:
        seg = int(sr * (rng.uniform(0.8, 3.5) if on else rng.uniform(0.5, 2.5)))
        if on:
            env[t : t + seg] = 1.0
        t += seg
        on = not on
    env = np.convolve(env, np.hanning(sr // 4) / (sr // 8), mode="same")
    return (noise * env * 0.3).astype(np.float32)


def make_pair(
    d0: float, drift_ppm: float, seed: int = 1, dur: float = DUR,
    mute: tuple[float, float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """(speaker, reference) を合成する。

    reference = 本人の声 + 他話者の声(独立ノイズ)
    speaker   = 本人の声を τ = d0 + (1+s)·t で読み出したもの(ドリフトは補間で生成)
    mute=(t0,t1) の間は本人が喋っていない(speaker はほぼ無音、ref は他話者のみ)
    """
    s = drift_ppm * 1e-6
    own = speechlike(seed)
    others = speechlike(seed + 100) + speechlike(seed + 200)
    if mute:
        i0, i1 = int(mute[0] * SR), int(mute[1] * SR)
        own[i0:i1] = 0.0
    ref = (own + others * 0.9).astype(np.float32)

    n_spk = int(dur * SR)
    tau = np.arange(n_spk) / SR                    # speaker 時刻
    t = (tau - d0) / (1 + s)                       # 対応する reference 時刻
    base_t = np.arange(len(own)) / SR
    spk = np.interp(t, base_t, own, left=0.0, right=0.0).astype(np.float32)
    spk += np.random.default_rng(seed + 999).standard_normal(n_spk).astype(np.float32) * 1e-4
    return spk, ref


def test_recover_offset_and_drift():
    spk, ref = make_pair(d0=1.234, drift_ppm=12.0)
    est = analysis.estimate_sync(spk, ref)
    assert abs(est.offset_s - 1.234) < 1e-3, f"offset {est.offset_s}"
    assert abs(est.drift_ppm - 12.0) < 1.0, f"drift {est.drift_ppm}"


def test_negative_offset_and_drift():
    spk, ref = make_pair(d0=-0.7, drift_ppm=-8.0, seed=2)
    est = analysis.estimate_sync(spk, ref)
    assert abs(est.offset_s - (-0.7)) < 1e-3
    assert abs(est.drift_ppm - (-8.0)) < 1.0


def test_silence_robustness():
    """中盤3分無音でも低信頼区間の除外でフィットが破綻しない(§7 手順4)。"""
    spk, ref = make_pair(d0=0.5, drift_ppm=20.0, seed=3, mute=(200.0, 380.0))
    est = analysis.estimate_sync(spk, ref)
    assert abs(est.offset_s - 0.5) < 1.5e-3
    assert abs(est.drift_ppm - 20.0) < 1.5
    assert est.used_segments < 10  # 実際に区間が除外されている


def test_round_trip_with_ffmpeg(tmp_path: Path):
    """rubberband の tempo 符号を実測で固定する往復テスト(§12.1)。"""
    d0, ppm = 1.234, 12.0
    spk, ref = make_pair(d0=d0, drift_ppm=ppm, seed=4)
    spk_wav = tmp_path / "spk.wav"
    _write_wav(spk_wav, spk)

    est = analysis.estimate_sync(spk, ref)
    program_samples = len(ref)
    filters = correction_filters(
        est.offset_s, est.drift_ppm, program_samples,
        drift_threshold_ppm=5.0, sr=SR,
    )
    out_wav = tmp_path / "corrected.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error",
         "-i", str(spk_wav), "-af", ",".join(filters), str(out_wav)],
        check=True,
    )
    corrected = _read_wav(out_wav)
    est2 = analysis.estimate_sync(corrected, ref)
    # 補正後: オフセット・区間残差 ±1ms 以内、ドリフト ±1ppm 以内(N-3)
    assert abs(est2.offset_s) < 1e-3, f"residual offset {est2.offset_s * 1e3:.3f}ms"
    assert abs(est2.drift_ppm) < 1.0, f"residual drift {est2.drift_ppm:.2f}ppm"
    resid = [abs(seg["lag_ms"]) for seg in est2.segments if seg["used"]]
    assert max(resid) < 1.0, f"segment residuals {resid}"


def test_no_drift_below_threshold(tmp_path: Path):
    """|s| < 閾値なら rubberband を通さずオフセットのみ補正する。"""
    filters = correction_filters(0.5, 2.0, 100000, drift_threshold_ppm=5.0, sr=SR)
    assert not any("rubberband" in f for f in filters)
    assert any("atrim=start_sample=4000" in f for f in filters)


def _write_wav(path: Path, x: np.ndarray) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error",
         "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
         "-c:a", "pcm_f32le", str(path)],
        input=x.astype(np.float32).tobytes(), check=True,
    )


def _read_wav(path: Path) -> np.ndarray:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
         "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)
