from __future__ import annotations

import math

import pytest

from app.pipeline.gain import GainPolicy, choose_gain


def policy(**params):
    return GainPolicy.from_params(params, "speaker_a")


def test_quiet_track_capped_but_attenuation_is_unbounded():
    result = choose_gain(-50, -30, policy())
    assert result.requested_gain_db == 30
    assert result.applied_gain_db == 12
    assert result.reasons == ("max_gain",)
    assert choose_gain(-5, -1, policy(target_lufs=-40)).applied_gain_db == -35


@pytest.mark.parametrize("lufs", [None, -math.inf, -70, -60])
def test_silent_or_unmeasurable_track_not_amplified(lufs):
    assert choose_gain(lufs, None, policy()).applied_gain_db == 0


def test_peak_headroom_takes_precedence_over_loudness_target():
    decision = choose_gain(-35, -2, policy())
    assert decision.applied_gain_db == pytest.approx(0.9)
    assert "peak_headroom" in decision.reasons
    # Even a largely silent file with a loud isolated event must be attenuated.
    assert choose_gain(-70, 3, policy()).applied_gain_db == pytest.approx(-4.1)


def test_individual_override_does_not_change_other_track():
    params = {"max_gain_db": 12, "track_max_gain_db": {"speaker_a": 24}}
    assert GainPolicy.from_params(params, "speaker_a").max_gain_db == 24
    assert GainPolicy.from_params(params, "speaker_b").max_gain_db == 12


@pytest.mark.parametrize("params", [
    {"max_gain_db": -1}, {"max_gain_db": 49}, {"max_gain_db": True},
    {"max_gain_db": "12"}, {"max_gain_db": math.nan}, {"target_lufs": math.inf},
    {"peak_ceiling_dbtp": 0}, {"silence_gate_lufs": -100},
    {"track_max_gain_db": []}, {"track_max_gain_db": {"speaker_a": math.nan}},
    {"track_max_gain_db": {"speaker_z": 1}},
])
def test_invalid_settings_rejected(params):
    with pytest.raises(ValueError):
        policy(**params)


@pytest.mark.parametrize("lufs,tp", [(-30, None), (-30, -math.inf), (-30, math.nan), (math.nan, -2), (math.inf, -2)])
def test_invalid_measurements_fail_closed(lufs, tp):
    with pytest.raises(ValueError):
        choose_gain(lufs, tp, policy())


@pytest.mark.parametrize("peak", ["nan", "inf"])
def test_nonfinite_true_peak_meter_is_rejected(monkeypatch, peak):
    from app.pipeline import ffmpeg
    monkeypatch.setattr(ffmpeg, "run", lambda *args: f"Summary:\n I: -30.0 LUFS\n Peak: {peak} dBFS")
    with pytest.raises(ffmpeg.FFmpegError):
        ffmpeg.measure_ebur128("unused.wav")


def test_silent_meter_peak_is_json_safe(monkeypatch):
    import json
    from app.pipeline import ffmpeg
    monkeypatch.setattr(ffmpeg, "run", lambda *args: "Summary:\n I: -70.0 LUFS\n Peak: -inf dBFS")
    measured = ffmpeg.measure_ebur128("unused.wav")
    assert measured["TP"] is None
    json.dumps(measured, allow_nan=False)
