from __future__ import annotations

import numpy as np
import pytest

from app.pipeline import ffmpeg
from app.pipeline.denoise import render_nr, verified_nr
from app.pipeline.voice import analyze_voice, bleed_pairs, nr_decision, nr_quality
from tests.audio_fixtures import add_noise, synthetic_speech, write_float


@pytest.mark.parametrize("seed", [1, 7, 29])
def test_vad_and_snr_known_speech(seed):
    clean, known = synthetic_speech(seed, sr=8000, duration=8)
    noisy = add_noise(clean, known, 20, seed+1)
    voice = analyze_voice(noisy, 8000)
    assert voice.reliable
    assert 12 < voice.snr_db < 28
    truth = known[:len(voice.power)*voice.hop].reshape(-1, voice.hop).mean(axis=1) > 0.5
    assert np.mean(voice.speech[truth]) > 0.8
    assert nr_decision(voice, "auto", 12)[0] <= 9
    damaged = noisy * 0.1
    qc = nr_quality(noisy, damaged, voice)
    assert qc["suspected"]
    assert qc["speech_band_loss_db"] == pytest.approx(20, abs=0.05)


def test_unreliable_vad_never_increases_nr():
    voice = analyze_voice(np.zeros(8000), 8000)
    assert not voice.reliable
    assert nr_decision(voice, "auto", 12)[0] == 0
    with pytest.raises(ValueError):
        nr_decision(voice, "invalid", 12)


def test_nonstationary_limits_explicit_and_auto_modes():
    clean, mask = synthetic_speech(sr=8000, duration=10)
    noisy = add_noise(clean, mask, 18)
    noisy[8000:10000] += np.random.default_rng(42).normal(0, 0.15, 2000)
    voice = analyze_voice(noisy, 8000)
    # Inject known coverage-independent nonstationarity to test the policy.
    voice.nonstationary = True
    for mode in ("auto", "light", "medium"):
        assert nr_decision(voice, mode, 12)[0] <= 3


@pytest.mark.parametrize("protected", [False, True])
def test_nr_graph_preserves_length_and_finite_output(tmp_path, protected):
    clean, mask = synthetic_speech(sr=48000)
    source, out = tmp_path/'source.wav', tmp_path/'out.wav'
    write_float(source, add_noise(clean, mask, 15))
    render_nr(source, out, 6, -45, protected)
    data = ffmpeg.decode_f32(out, rate=48000)
    assert len(data) == len(clean)
    assert np.isfinite(data).all()
    result = verified_nr(source, out, "auto", 12, protected)
    assert result['nr_db'] <= 9
    assert len(result['attempts']) <= 3
    assert not result['quality']['suspected']


def test_nr_off_is_identity(tmp_path):
    clean, _ = synthetic_speech()
    source, out = tmp_path/'in.wav', tmp_path/'out.wav'
    write_float(source, clean)
    render_nr(source, out, 0, -60, True)
    assert np.array_equal(ffmpeg.decode_f32(out, rate=48000), clean)


def test_delayed_and_inverted_bleed_is_detected():
    clean, _ = synthetic_speech(sr=8000)
    delayed = np.concatenate([np.zeros(32), clean[:-32]]) * -0.3
    result = bleed_pairs({'a': clean, 'b': delayed})
    assert len(result) == 1
    assert result[0]['correlation'] < -0.9
    assert abs(abs(result[0]['lag_ms']) - 4) <= 0.3
    unrelated = np.random.default_rng(8).normal(0, .1, len(clean))
    assert bleed_pairs({'a': clean, 'b': unrelated}) == []


def test_degradation_retry_always_reads_original(tmp_path, monkeypatch):
    import app.pipeline.denoise as module
    clean, mask = synthetic_speech()
    source, out = tmp_path/'in.wav', tmp_path/'out.wav'
    write_float(source, add_noise(clean, mask, 20))
    calls = []
    original = module.render_nr
    def render(src, dest, amount, noise, protection):
        calls.append((src, amount))
        original(src, dest, amount, noise, protection)
    monkeypatch.setattr(module, 'render_nr', render)
    monkeypatch.setattr(module, 'nr_quality', lambda *args: {'suspected': True, 'valid': True})
    result = module.verified_nr(source, out, 'medium', 12, True)
    assert result['nr_db'] == 0
    assert all(path == source for path, _ in calls)
    assert len(calls) <= 3
    assert np.array_equal(ffmpeg.decode_f32(source, rate=48000), ffmpeg.decode_f32(out, rate=48000))


def test_speech_band_protection_reduces_speech_loss(tmp_path):
    from app.pipeline.voice import nr_quality
    clean, mask = synthetic_speech(sr=48000)
    source = tmp_path/'in.wav'
    write_float(source, add_noise(clean, mask, 15) * .1)
    before = ffmpeg.decode_f32(source, rate=8000)
    voice = analyze_voice(before, 8000)
    losses = []
    for protected in (False, True):
        out = tmp_path/f'{protected}.wav'
        render_nr(source, out, 12, -20, protected)
        losses.append(nr_quality(before, ffmpeg.decode_f32(out, rate=8000), voice)['speech_band_loss_db'])
    assert losses[1] < losses[0]


@pytest.mark.parametrize('protected', [False, True])
def test_nr_compensates_latency_including_tail(tmp_path, protected):
    from app.pipeline.analysis import gcc_phat
    rng = np.random.default_rng(123)
    samples = rng.normal(0, .05, 48000*2+137).astype(np.float32)
    # Include an event near the end so truncating the NR/FFT tail is observable.
    samples[-100] = .8
    source, out = tmp_path/'in.wav', tmp_path/'out.wav'
    write_float(source, samples)
    render_nr(source, out, 6, -80, protected)
    processed = ffmpeg.decode_f32(out, rate=48000)
    assert len(processed) == len(samples)
    assert abs(gcc_phat(processed, samples, max_lag=3000)[0]) < .1
    assert np.argmax(np.abs(processed[-500:])) == 400


@pytest.mark.parametrize('seed', [1, 7, 29])
@pytest.mark.parametrize('kind', ['moving', 'keyboard'])
def test_real_nonstationary_noise_caps_nr(tmp_path, seed, kind):
    clean, mask = synthetic_speech(seed, duration=8)
    noisy = add_noise(clean, mask, 20, seed+100)
    if kind == 'moving':
        t = np.arange(len(clean))/48000
        noisy += np.random.default_rng(seed+300).normal(size=len(clean)) * (.002+.04*np.sin(2*np.pi*t*.7)**8)
    else:
        for start in (10000, 125000, 245000):
            noisy[start:start+1500] += np.random.default_rng(seed+start).normal(0,.08,1500)
    source = tmp_path/'noise.wav'
    write_float(source, noisy)
    voice = analyze_voice(ffmpeg.decode_f32(source, rate=8000),8000)
    assert voice.nonstationary
    assert nr_decision(voice, 'auto', 12)[0] <= 3
