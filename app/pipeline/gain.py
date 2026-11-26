"""Bound Cleanup's linear gain before encoding fixed-point audio.

This module only chooses parameters; FFmpeg remains the audio processor.
No limiter or automatic makeup gain is introduced.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Mapping

# ebur128 summaries are rounded to 0.1 dB. Reserve room for that rounding
# and the final 24-bit quantization instead of rounding the applied gain up.
METER_MARGIN_DB = 0.1


def finite_setting(value: object, name: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} は数値で指定してください")
    number = float(value)
    if not math.isfinite(number) or not lower <= number <= upper:
        raise ValueError(f"{name} は {lower}〜{upper} の有限値で指定してください")
    return number


@dataclass(frozen=True)
class GainPolicy:
    target_lufs: float
    max_gain_db: float
    silence_gate_lufs: float
    peak_ceiling_dbtp: float

    @classmethod
    def from_params(cls, params: Mapping[str, object], role: str) -> GainPolicy:
        cap = finite_setting(params.get("max_gain_db", 12.0), "max_gain_db", 0, 48)
        overrides = params.get("track_max_gain_db", {})
        if not isinstance(overrides, dict):
            raise ValueError("track_max_gain_db は話者ごとの数値で指定してください")
        for name, value in overrides.items():
            if name not in ("speaker_a", "speaker_b", "speaker_c"):
                raise ValueError(f"不明な話者: {name}")
            finite_setting(value, f"track_max_gain_db.{name}", 0, 48)
        if role in overrides:
            cap = float(overrides[role])
        return cls(
            target_lufs=finite_setting(params.get("target_lufs", -20.0), "target_lufs", -70, 0),
            max_gain_db=cap,
            silence_gate_lufs=finite_setting(
                params.get("silence_gate_lufs", -60.0), "silence_gate_lufs", -90, -30,
            ),
            peak_ceiling_dbtp=finite_setting(
                params.get("peak_ceiling_dbtp", -1.0), "peak_ceiling_dbtp", -12, -0.1,
            ),
        )

    def report(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class GainDecision:
    requested_gain_db: float | None
    applied_gain_db: float
    reasons: tuple[str, ...]


def choose_gain(
    measured_lufs: float | None, true_peak_dbtp: float | None, policy: GainPolicy,
) -> GainDecision:
    """Cap amplification, never necessary attenuation; preserve peak headroom.

    ebur128's silence sentinel (-70 LUFS) is below the default silence gate.
    Missing/-inf peak is accepted only for a track that will not be boosted.
    NaN/+inf or missing peak on measurable speech fails closed.
    """
    reasons: list[str] = []
    requested: float | None = None
    if measured_lufs is None or measured_lufs == -math.inf:
        gain = 0.0
        reasons.append("loudness_unmeasurable")
    elif not math.isfinite(measured_lufs):
        raise ValueError("音量測定が非有限値です")
    elif measured_lufs <= policy.silence_gate_lufs:
        gain = 0.0
        reasons.append("below_silence_gate")
    else:
        requested = policy.target_lufs - measured_lufs
        gain = min(requested, policy.max_gain_db)
        if gain < requested:
            reasons.append("max_gain")

    if true_peak_dbtp is None or true_peak_dbtp == -math.inf:
        if requested is not None:
            raise ValueError("True Peak を測定できないためゲイン整合を停止しました")
    elif not math.isfinite(true_peak_dbtp):
        raise ValueError("True Peak 測定が非有限値です")
    else:
        headroom = policy.peak_ceiling_dbtp - true_peak_dbtp - METER_MARGIN_DB
        if headroom < gain:
            gain = headroom
            reasons.append("peak_headroom")
    return GainDecision(requested, gain, tuple(reasons))
