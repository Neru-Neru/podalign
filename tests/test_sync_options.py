from __future__ import annotations

import numpy as np
import pytest

from app import project as prj, runner
from app.pipeline import analysis, ffmpeg
from app.pipeline.robust import fit_clock
from app.pipeline.s1_sync import correction_filters
from tests.audio_fixtures import write_float
from tests.test_sync import make_pair


@pytest.mark.parametrize('method', ['theil_sen', 'ransac'])
def test_robust_fit_rejects_known_outliers(method):
    t = np.arange(12) * 30.0
    lag = 1.234 + t * 12e-6
    lag[[2, 7, 9]] += [0.3, -0.1, 0.6]
    slope, offset, mask = fit_clock(t, lag, np.ones(12), np.ones(12, dtype=bool), method)
    assert abs(slope * 1e6 - 12) < 0.01
    assert abs(offset - 1.234) < 1e-6
    assert mask.sum() == 9


@pytest.mark.parametrize('anchor', ['uniform', 'vad'])
@pytest.mark.parametrize('method', ['theil_sen', 'ransac'])
def test_anchor_estimator_recovers_known_timing(anchor, method):
    speaker, ref = make_pair(.35, 20, seed=19, dur=120)
    # make_pair builds a 600s reference; limit both to the evaluated duration.
    ref = ref[:len(speaker)]
    est = analysis.estimate_sync(speaker, ref, n_segments=8, segment_s=8,
                                 anchor_mode=anchor, fit_method=method)
    assert abs(est.offset_s-.35) < .0015
    assert abs(est.drift_ppm-20) < 2
    assert est.quality['safe']


@pytest.mark.parametrize('method', ['rubberband', 'polyphase'])
@pytest.mark.parametrize('ppm', [-100, 12, 100])
def test_resampler_applies_known_ratio(tmp_path, method, ppm):
    sr = 8000
    n = sr * 20
    t = np.arange(n) / sr
    source = tmp_path/'in.wav'
    write_float(source, np.sin(2*np.pi*440*t).astype(np.float32)*.3, sr)
    out = tmp_path/'out.wav'
    # No trim/pad here: measure actual ratio, not the requested final duration.
    filters = correction_filters(0, ppm, n, 5, sr, method)
    filters = [f for f in filters if not f.startswith(('atrim=', 'apad='))]
    ffmpeg.run(['-i', str(source), '-af', ','.join(filters), '-c:a', 'pcm_f32le', str(out)])
    data = ffmpeg.decode_f32(out, rate=sr)
    assert abs(len(data) - n/(1+ppm*1e-6)) < 3
    assert np.isfinite(data).all()


def test_unsafe_sync_cannot_be_approved_through_api(data_dir):
    doc = prj.create_project('blocked')
    with prj.update(doc['id']) as saved:
        saved['stages']['sync']['status'] = 'done'
        saved['stages']['sync']['input_fingerprint'] = prj.expected_fingerprints(saved)['sync']
        saved['stages']['sync']['report'] = {'approval_blocked': True}
    with pytest.raises(runner.NotRunnable, match='信頼度'):
        runner.approve(doc['id'], 'sync')


def test_jump_cannot_be_accepted_as_constant_clock_error():
    speaker, ref = make_pair(.35, 20, seed=5, dur=120)
    speaker[len(speaker)//2:] = np.roll(speaker[len(speaker)//2:], 80)
    est = analysis.estimate_sync(speaker, ref, n_segments=8, segment_s=8)
    assert not est.quality['safe']


@pytest.mark.parametrize('params', [
    {'anchor_mode': 'bad'}, {'fit_method': 'bad'}, {'resample_method': 'bad'},
    {'n_segments': 3.5}, {'drift_threshold_ppm': float('nan')},
    {'allow_low_confidence': 1}, {'manual_offsets_ms': {'speaker_z': 1}},
])
def test_sync_parameter_validation(params):
    from app.pipeline.s1_sync import validate_params
    base = {'n_segments': 8, 'segment_s': 8, 'drift_threshold_ppm': 5, **params}
    with pytest.raises(ValueError):
        validate_params(base, ['speaker_a', 'speaker_b'])


def test_verification_failure_needs_explicit_override():
    from app.pipeline.s1_sync import review_safety
    estimates = {'speaker_a': analysis.SyncEstimate(0, 0, quality={'safe': True})}
    verified = {'speaker_a': {'max_segment_residual_ms': None, 'quality': {'safe': False}}}
    assert review_safety(estimates, verified, False) == (['speaker_a'], True)
    assert review_safety(estimates, verified, True) == (['speaker_a'], False)


def test_manual_offset_recovers_failed_auto_without_claiming_confidence(monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from app.pipeline.s1_sync import _estimate
    ref = np.random.default_rng(1).normal(0, .01, 8000*60).astype(np.float32)
    own = np.random.default_rng(2).normal(0, .01, len(ref)).astype(np.float32)
    monkeypatch.setattr(ffmpeg, 'decode_f32', lambda *args, **kwargs: own)
    ctx = SimpleNamespace(progress=lambda *args: None, upstream_dir=lambda *args: Path('/tmp'), speakers=['speaker_a'])
    est = _estimate(ctx, 'speaker_a', ref, {'n_segments': 8, 'segment_s': 5,
                    'manual_offsets_ms': {'speaker_a': 1234}})
    assert est.offset_s == 1.234
    assert not est.quality['safe']
    assert est.drift_ppm == 0


@pytest.mark.parametrize('method', ['rubberband', 'polyphase'])
def test_high_frequency_energy_is_preserved_at_48k(tmp_path, method):
    rate = 48000
    t = np.arange(rate*2)/rate
    signal = sum(np.sin(2*np.pi*f*t)*.1 for f in (5000,12000,20000)).astype(np.float32)
    source, out = tmp_path/'high.wav', tmp_path/'out.wav'
    write_float(source, signal, rate)
    ffmpeg.run(['-i',str(source),'-af',','.join(correction_filters(0,100,len(signal),5,rate,method)),
                '-c:a','pcm_f32le',str(out)])
    decoded = ffmpeg.decode_f32(out,rate=rate)
    ratio = np.mean(decoded[rate//4:-rate//4]**2)/np.mean(signal[rate//4:-rate//4]**2)
    assert abs(10*np.log10(ratio)) < .5



def test_vad_anchors_are_independent_when_windows_overlap():
    speaker, ref = make_pair(.35, 12, seed=41, dur=60)
    est = analysis.estimate_sync(speaker, ref, n_segments=10, segment_s=15, anchor_mode='vad')
    times = [s['t'] for s in est.segments]
    assert len(times) == len(set(times))
    assert abs(est.offset_s-.35) < .0015
