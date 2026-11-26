"""Original, deterministic synthetic signals and metrics; no external recordings.

The voiced/unvoiced components and speech mask are known independently of VAD.
These signals validate processing machinery, not perceptual speech quality.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float32]


def synthetic_speech(seed: int = 0, sr: int = 48000, duration: float = 6.0) -> tuple[FloatArray, npt.NDArray[np.bool_]]:
    rng = np.random.default_rng(seed)
    t = np.arange(round(sr * duration)) / sr
    # Smooth syllabic bursts; include low fundamentals and high consonant energy.
    envelope = np.sin(np.pi * np.clip((t % 1.2 - 0.2) / 0.7, 0, 1)) ** 2
    phase = 2 * np.pi * (110 * t + 0.5 * np.sin(2 * np.pi * 0.7 * t))
    voice = sum(np.sin(k * phase) / k for k in range(1, 24))
    noise = rng.standard_normal(len(t))
    unvoiced = np.concatenate(([0.0], np.diff(noise))) * 0.04
    clean = envelope * (voice + unvoiced)
    clean *= 0.35 / max(float(np.max(np.abs(clean))), 1e-12)
    return clean.astype(np.float32), envelope > 0.05


def add_noise(clean: FloatArray, speech: npt.NDArray[np.bool_], snr_db: float, seed: int = 0) -> FloatArray:
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(len(clean))
    power = float(np.mean(clean[speech].astype(np.float64) ** 2))
    scale = np.sqrt(power / (10 ** (snr_db / 10) * np.mean(noise**2)))
    return (clean + noise * scale).astype(np.float32)


def write_float(path: Path, samples: FloatArray, sr: int = 48000) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "f32le", "-ar", str(sr),
         "-ac", "1", "-i", "-", "-c:a", "pcm_f32le", str(path)],
        input=samples.tobytes(), check=True,
    )


def metrics(clean: FloatArray, processed: FloatArray, speech: npt.NDArray[np.bool_], sr: int) -> dict[str, float | int]:
    if len(clean) != len(processed) or len(clean) != len(speech):
        raise ValueError("Metrics require equal lengths and a fixed speech mask")
    if not np.all(np.isfinite(processed)):
        raise ValueError("Non-finite output")
    before = clean.astype(np.float64)
    after = processed.astype(np.float64)
    # Fixed-mask, gain-invariant projection error (SI-SDR). Gain-sensitive band
    # change is reported separately, so normalization cannot hide attenuation.
    source = before[speech]
    estimate = after[speech]
    scale = float(np.dot(estimate, source) / (np.dot(source, source) + 1e-20))
    target = source * scale
    error = estimate - target
    si_sdr = float(10 * np.log10((np.dot(target, target) + 1e-20) / (np.dot(error, error) + 1e-20)))
    # Windowed FFT power over *speech only*, with a fixed 300–3400 Hz mask.
    win = sr // 50
    n = len(before) // win
    frame_mask = speech[:n * win].reshape(n, win).mean(axis=1) >= 0.8
    window = np.hanning(win)
    band = (np.fft.rfftfreq(win, 1 / sr) >= 300) & (np.fft.rfftfreq(win, 1 / sr) <= 3400)
    def power(x: npt.NDArray[np.float64]) -> float:
        frames = x[:n * win].reshape(n, win)[frame_mask]
        return float(np.sum(np.abs(np.fft.rfft(frames * window, axis=1)[:, band]) ** 2))
    return {
        "si_sdr_db": si_sdr,
        "speech_band_change_db": float(10 * np.log10((power(after) + 1e-20) / (power(before) + 1e-20))),
        "sample_peak": float(np.max(np.abs(after))),
        "rail_samples": int(np.count_nonzero(np.abs(after) >= 1.0 - 2**-23)),
        "samples": len(after),
    }
