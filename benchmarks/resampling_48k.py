"""48kHz production-rate comparison; 8kHz independent verification."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.pipeline import analysis, ffmpeg
from app.pipeline.s1_sync import correction_filters
from benchmarks.pipeline_comparison import measured_ffmpeg
from tests.audio_fixtures import write_float
from tests.test_sync import make_pair


def compare(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for ppm in (-100, -12, 12, 100):
        speaker, reference = make_pair(.35, ppm, seed=71, dur=120)
        base = output / f'base_{ppm}.wav'
        source = output / f'input_{ppm}.wav'
        write_float(base, speaker, 8000)
        ffmpeg.run(['-i', str(base), '-ar', '48000', '-c:a', 'pcm_f32le', str(source)])
        for method in ('rubberband', 'polyphase'):
            dest = output / f'{ppm}_{method}.wav'
            filters = correction_filters(.35, ppm, 120*48000, 5, 48000, method)
            elapsed, rss = measured_ffmpeg(['-i', str(source), '-af', ','.join(filters), '-c:a', 'pcm_f32le', str(dest)])
            corrected = ffmpeg.decode_f32(dest, rate=8000)
            est = analysis.estimate_sync(corrected, reference, n_segments=8, segment_s=8)
            rows.append({'ppm': ppm, 'method': method, 'sample_rate': 48000, 'duration_seconds': 120,
                         'seconds': elapsed, 'rss_kib_sampled': rss,
                         'output_samples': round(ffmpeg.probe(dest)['duration']*48000),
                         'residual_offset_ms': est.offset_s*1000, 'residual_ppm': est.drift_ppm,
                         'max_segment_ms': max(abs(s['lag_ms']) for s in est.segments if s['used'])})
    result = {'verification_rate': 8000, 'rows': rows,
              'note': 'ピークRSSは5ms間隔のサンプリング値。実行時間は環境依存。'}
    (output/'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.output)
    print(len(result['rows']))
