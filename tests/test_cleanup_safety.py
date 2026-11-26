"""Real FFmpeg regression tests including intermediate fixed-point clipping."""
from __future__ import annotations

import json

import numpy as np
import pytest

from app import project as prj, storage
from app.pipeline import ffmpeg, s2_cleanup
from app.pipeline.base import StageContext
from tests.audio_fixtures import add_noise, metrics, synthetic_speech, write_float


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("kind", ["quiet", "silence", "transient", "normal", "noise"])
def test_cleanup_measures_before_fixed_point_encoding(data_dir, monkeypatch, kind, legacy):
    doc = prj.create_project("安全性 A/B")
    doc["assets"]["speaker_a"] = {}
    if legacy:
        doc["stage_order"].remove("trim")
    src_dir = storage.stage_dir(doc["id"], "sync" if legacy else "trim")
    src_dir.mkdir(parents=True)
    clean, speech = synthetic_speech(seed=41)
    x = clean.copy()
    if kind == "quiet":
        x *= 0.005
    elif kind == "silence":
        x[:] = 0
    elif kind == "transient":
        x *= 0.05
        x[48000:48010] = 0.95
    elif kind == "noise":
        x = add_noise(x, speech, 10, seed=42)
    source = src_dir / "speaker_a.flac"
    wav = src_dir / "source.wav"
    write_float(wav, x)
    ffmpeg.run(["-i", str(wav), *ffmpeg.FLAC24, str(source)])
    # Deliberately create an over-range pre-gain signal using FFmpeg. The
    # previous FLAC intermediate clipped it before measurement/attenuation.
    monkeypatch.setattr(s2_cleanup, "build_chain", lambda *args: "volume=4")
    ctx = StageContext(doc["id"], "cleanup", doc)
    monkeypatch.setattr(ctx, "make_preview", lambda *args, **kwargs: None)
    monkeypatch.setattr(ctx, "make_peaks", lambda *args: None)
    params = {**prj.DEFAULT_PARAMS["cleanup"], "target_lufs": -10}
    if legacy:
        for key in ("max_gain_db", "track_max_gain_db", "silence_gate_lufs", "peak_ceiling_dbtp"):
            params.pop(key)
    report = s2_cleanup.run(ctx, params)
    track = report["tracks"]["speaker_a"]
    out = ffmpeg.decode_f32(ctx.out_dir / "speaker_a.flac", rate=48000)
    assert len(out) == len(x)
    assert np.all(np.isfinite(out))
    assert np.count_nonzero(np.abs(out) >= 1) == 0
    assert track["align_gain_db"] <= 12
    assert track["true_peak_after_gain"] is None or track["true_peak_after_gain"] <= -1
    assert not list(ctx.out_dir.glob("*_pregain*"))
    json.dumps(report, allow_nan=False)
    if kind == "quiet":
        assert "max_gain" in track["gain_limit_reasons"]
    elif kind == "silence":
        assert track["align_gain_db"] == 0
        assert np.all(out == 0)
    elif kind == "normal":
        assert track["true_peak_before_gain"] > 0
        quality = metrics(x, out, speech, 48000)
        assert quality["si_sdr_db"] > 100  # gain only, no pre-gain clipping


def test_metrics_use_known_reference_and_fixed_mask():
    clean, mask = synthetic_speech(seed=3)
    noisy = add_noise(clean, mask, 10, seed=4)
    a = metrics(clean, clean * 0.5, mask, 48000)
    b = metrics(clean, noisy, mask, 48000)
    assert a["speech_band_change_db"] == pytest.approx(-6.0206, abs=0.01)
    assert a["si_sdr_db"] > 100
    assert b["si_sdr_db"] == pytest.approx(10, abs=0.3)


@pytest.mark.parametrize('mode', ['off', 'auto', 'light', 'medium'])
def test_complete_cleanup_with_nr_selection(data_dir, monkeypatch, mode):
    doc = prj.create_project('NR一気通貫')
    doc['assets']['speaker_a'] = {}
    directory = storage.stage_dir(doc['id'], 'trim')
    directory.mkdir(parents=True)
    clean, speech = synthetic_speech(seed=11)
    write_float(directory/'input.wav', add_noise(clean, speech, 15, seed=12))
    ffmpeg.run(['-i', str(directory/'input.wav'), *ffmpeg.FLAC24, str(directory/'speaker_a.flac')])
    ctx = StageContext(doc['id'], 'cleanup', doc)
    monkeypatch.setattr(ctx, 'make_preview', lambda *args, **kwargs: None)
    monkeypatch.setattr(ctx, 'make_peaks', lambda *args: None)
    # Per-track override must work even when the common setting is legacy.
    params = {**prj.DEFAULT_PARAMS['cleanup'], 'track_denoise_mode': {'speaker_a': mode}}
    report = s2_cleanup.run(ctx, params)
    track = report['tracks']['speaker_a']
    assert track['denoise']['requested_mode'] == mode
    assert not track['denoise']['quality']['suspected']
    assert track['true_peak_after_gain'] <= -1
    assert len(ffmpeg.decode_f32(ctx.out_dir/'speaker_a.flac', rate=48000)) == len(clean)
    assert not list(ctx.out_dir.glob('_*.wav'))
    json.dumps(report, allow_nan=False)
