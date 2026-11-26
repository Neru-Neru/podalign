"""NumPy-only analysis; never transforms delivered audio. Original implementation."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class VoiceAnalysis:
    speech: np.ndarray
    power: np.ndarray
    hop: int
    sr: int
    noise_db: float
    snr_db: float | None
    reliable: bool
    nonstationary: bool
    variation: float
    transient_fraction: float

    def report(self) -> dict:
        return {"noise_floor_db": self.noise_db, "snr_db": self.snr_db,
                "reliable": self.reliable, "speech_fraction": float(self.speech.mean()) if self.speech.size else 0,
                "nonstationary": self.nonstationary, "variation": self.variation,
                "transient_fraction": self.transient_fraction}


def analyze_voice(x: np.ndarray, sr: int) -> VoiceAnalysis:
    if sr < 8000 or x.ndim != 1 or not np.all(np.isfinite(x)):
        raise ValueError("音声解析には8kHz以上の有限なモノ音声が必要です")
    hop = max(1, round(sr * 0.02))
    n = len(x) // hop
    power = np.empty(n)
    flatness = np.empty(n)
    # Bound FFT working memory, including multi-hour recordings.
    for start in range(0, n, 2048):
        end = min(n, start + 2048)
        frames = x[start * hop:end * hop].reshape(-1, hop).astype(np.float64)
        power[start:end] = np.mean(frames**2, axis=1)
        spec = np.abs(np.fft.rfft(frames * np.hanning(hop), axis=1)) ** 2 + 1e-20
        flatness[start:end] = np.exp(np.mean(np.log(spec), axis=1)) / np.mean(spec, axis=1)
    db = 10 * np.log10(power + 1e-20)
    noise = float(np.percentile(db, 20)) if n else -200.0
    # Tonal structure supports voiced speech; energetic broadband bursts can
    # still be unvoiced speech. Coverage checks prevent pretending this is ML VAD.
    speech = (db > noise + 8) & ((flatness < 0.65) | (db > noise + 15)) & (db > -75)
    if n:
        # Bridge <=100ms pauses, retaining consonants rather than cutting audio.
        idx = np.flatnonzero(speech)
        for a, b in zip(idx[:-1], idx[1:]):
            if b - a <= 6:
                speech[a:b] = True
    quiet = power[~speech]
    voiced = power[speech]
    reliable = bool(len(quiet) >= 25 and len(voiced) >= 25 and np.percentile(db, 90) - noise >= 8)
    noise_power = float(np.median(quiet)) if quiet.size else 0
    snr = float(10 * np.log10((np.mean(voiced) + 1e-20) / (noise_power + 1e-20))) if reliable else None
    variation = float(np.std(quiet) / (np.mean(quiet) + 1e-20)) if quiet.size else 0.0
    transients = float(np.mean(quiet > max(noise_power * 10, 1e-16))) if quiet.size else 0.0
    # An energy VAD can call a loud noise burst "speech". Inspect a second,
    # independent broadband set as well, rather than only VAD-negative frames.
    broadband = power[flatness > .35]
    bed_variation = float(np.std(broadband) / (np.mean(broadband) + 1e-20)) if len(broadband) >= 25 else 0.0
    jumps = (np.diff(db) > 8) & (flatness[1:] > .3)
    variation = max(variation, bed_variation)
    transient_detected = int(jumps.sum()) >= 2
    transients = max(transients, float(jumps.mean()) if jumps.size else 0.0)
    return VoiceAnalysis(speech, power, hop, sr, float(10 * np.log10(noise_power + 1e-20)),
                         snr, reliable, variation > 1.0 or transients > 0.05 or transient_detected, variation, transients)


def nr_decision(voice: VoiceAnalysis, mode: str, maximum: float) -> tuple[float, list[str]]:
    if mode not in ("legacy", "off", "auto", "light", "medium"):
        raise ValueError("ノイズ除去モードが不正です")
    if not np.isfinite(maximum) or not 0 <= maximum <= 12:
        raise ValueError("ノイズ除去上限は0〜12dBです")
    reasons: list[str] = []
    if mode == "legacy":
        return maximum, reasons
    if mode == "off":
        return 0.0, reasons
    if not voice.reliable or voice.snr_db is None:
        return 0.0, ["発話・非発話区間が不足しているためノイズ除去を省略"]
    amount = min(maximum, 6 if mode == "light" else 9)
    if mode == "auto":
        amount = 0.0 if voice.snr_db >= 30 else min(maximum, 6 if voice.snr_db >= 25 else 9)
    if voice.nonstationary or voice.snr_db < 10:
        amount = min(amount, 3)
        reasons.append("変動ノイズまたは低SNRのためノイズ除去を3dB以下に制限")
    return amount, reasons


def nr_quality(before: np.ndarray, after: np.ndarray, voice: VoiceAnalysis) -> dict:
    """Freeze pre-NR masks. Compare NR only, excluding HPF/gate/gain effects."""
    n = len(voice.power)
    if len(before) != len(after) or len(before) // voice.hop != n or not np.all(np.isfinite(after)):
        return {"valid": False, "suspected": True, "reason": "音声長または有限値の検証失敗"}
    if not voice.reliable:
        return {"valid": False, "suspected": False, "reason": "発話区間の根拠不足"}
    band_before, band_after, p_after = np.zeros(n), np.zeros(n), np.zeros(n)
    freq = np.fft.rfftfreq(voice.hop, 1 / voice.sr)
    band = (freq >= 300) & (freq <= 3400)
    for start in range(0, n, 2048):
        end = min(n, start + 2048)
        b = before[start * voice.hop:end * voice.hop].reshape(-1, voice.hop).astype(np.float64)
        a = after[start * voice.hop:end * voice.hop].reshape(-1, voice.hop).astype(np.float64)
        window = np.hanning(voice.hop)
        band_before[start:end] = np.sum(np.abs(np.fft.rfft(b * window, axis=1)[:, band])**2, axis=1)
        band_after[start:end] = np.sum(np.abs(np.fft.rfft(a * window, axis=1)[:, band])**2, axis=1)
        p_after[start:end] = np.mean(a**2, axis=1)
    def change(a, b, mask):
        return float(10 * np.log10((np.mean(a[mask]) + 1e-20) / (np.mean(b[mask]) + 1e-20)))
    loss = -change(band_after, band_before, voice.speech)
    improvement = change(p_after, voice.power, voice.speech) - change(p_after, voice.power, ~voice.speech)
    return {"valid": True, "speech_band_loss_db": loss, "snr_improvement_db": improvement,
            "suspected": loss > 3.0 or improvement < -0.5}


def bleed_pairs(tracks: dict[str, np.ndarray], sr: int = 8000) -> list[dict]:
    """Lag-aware frame correlations; detection only, never auto-removes audio."""
    results = []
    names = list(tracks)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            a, b = tracks[first], tracks[second]
            win = sr // 4
            # Bounded, uniformly distributed windows; no whole-file FFT matrix.
            starts = np.linspace(0, max(0, min(len(a), len(b)) - win - sr // 50), 120, dtype=int)
            matches = []
            for start in np.unique(starts):
                x, y = a[start:start + win].astype(float), b[start:start + win].astype(float)
                if len(x) != win or len(y) != win or min(np.mean(x*x), np.mean(y*y)) < 1e-10:
                    continue
                best, lag = 0.0, 0
                for delay in range(-sr // 100, sr // 100 + 1, 2):
                    xx, yy = (x[delay:], y[:win-delay]) if delay >= 0 else (x[:win+delay], y[-delay:])
                    xx, yy = xx - xx.mean(), yy - yy.mean()
                    corr = float(np.dot(xx, yy) / (np.linalg.norm(xx) * np.linalg.norm(yy) + 1e-20))
                    if abs(corr) > abs(best):
                        best, lag = corr, delay
                matches.append((best, lag))
            if matches:
                support = [m for m in matches if abs(m[0]) >= 0.75]
                fraction = len(support) / len(matches)
                if len(support) >= 3 and fraction >= 0.2:
                    results.append({"tracks": [first, second], "correlation": float(np.median([m[0] for m in support])),
                                    "lag_ms": float(np.median([m[1] for m in support]) * 1000 / sr),
                                    "support_fraction": fraction, "windows": len(matches)})
    return results
