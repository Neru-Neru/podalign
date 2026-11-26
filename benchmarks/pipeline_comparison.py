"""Original multi-seed comparison of NR, HPF, anchors, fits and resamplers.

Run with --output DIR. Writes JSON measurements and A/B audio, not quality claims.
"""
from __future__ import annotations

import argparse
import json
import time
import subprocess
import tempfile
import hashlib
from pathlib import Path

import numpy as np

from app.pipeline import analysis, ffmpeg
from app.pipeline.denoise import render_nr, verified_nr
from app.pipeline.robust import fit_clock
from app.pipeline.s1_sync import correction_filters
from app.pipeline.voice import analyze_voice, bleed_pairs
from tests.audio_fixtures import add_noise, metrics, synthetic_speech, write_float
from tests.test_sync import make_pair


def measured_ffmpeg(args: list[str]) -> tuple[float, int]:
    """Linux RSS samples at 5ms; approximate process peak, not total machine RAM."""
    peak = 0
    started = time.perf_counter()
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen([ffmpeg.FFMPEG, "-hide_banner", "-nostdin", "-y", *args],
                                stdout=subprocess.DEVNULL, stderr=log)
        while proc.poll() is None:
            try:
                status = Path(f"/proc/{proc.pid}/status").read_text()
                line = next((s for s in status.splitlines() if s.startswith("VmRSS:")), "VmRSS: 0")
                peak = max(peak, int(line.split()[1]))
            except (FileNotFoundError, ProcessLookupError):
                pass
            time.sleep(.005)
        if proc.returncode:
            log.seek(0)
            raise ffmpeg.FFmpegError(args, log.read().decode(errors="replace"))
    return time.perf_counter() - started, peak


def run(output: Path, seeds: list[int] | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
    code = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path("app/pipeline").glob("*.py"))}
    results: dict = {"ffmpeg": version, "source_sha256": code, "nr": [], "highpass": [], "sync": [], "fits": [], "resampling": [], "bleed": [],
                     "seeds": seeds if seeds is not None else [1, 7, 29], "sample_rate": 48000,
                     "limitations": "合成音声による測定。聴感上の改善・実録音への一般化は未判定。"}
    for seed in results['seeds']:
        clean, mask = synthetic_speech(seed, duration=8)
        cases = {'clean': clean, **{f'snr_{snr}': add_noise(clean, mask, snr, seed+100) for snr in (0, 10, 20, 30, 40)}}
        moving = add_noise(clean, mask, 20, seed+200)
        t = np.arange(len(clean))/48000
        moving += np.random.default_rng(seed+300).normal(size=len(clean)) * (.002+.04*(np.sin(2*np.pi*t*.7)**8))
        cases['moving'] = moving.astype(np.float32)
        clicks = cases['snr_20'].copy()
        for start in (10000, 125000, 245000):
            clicks[start:start+1500] += np.random.default_rng(seed+start).normal(0, .08, 1500)
        cases['keyboard'] = clicks
        for case, audio in cases.items():
            source = output/f'nr_{seed}_{case}_input.wav'
            write_float(source, audio)
            voice = analyze_voice(ffmpeg.decode_f32(source, rate=8000), 8000)
            for mode, protect in [('legacy', False), ('off', True), ('auto', True), ('light', True), ('medium', True), ('medium', False)]:
                dest = output/f'nr_{seed}_{case}_{mode}_{protect}.wav'
                started = time.perf_counter()
                if mode == 'legacy':
                    render_nr(source, dest, 12, voice.noise_db, False)
                    report = {'nr_db': 12, 'attempts': [], 'analysis': voice.report()}
                else:
                    report = verified_nr(source, dest, mode, 12, protect)
                elapsed = time.perf_counter()-started
                decoded = ffmpeg.decode_f32(dest, rate=48000)
                results['nr'].append({'seed':seed,'case':case,'mode':mode,'protected':protect,
                                      'seconds':elapsed,'decision':report,'metrics':metrics(clean,decoded,mask,48000)})
        # Same clean source with additive low-frequency rumble; report both
        # removal and changes to known low fundamentals, not just speech band.
        rumble = (clean + .03*np.sin(2*np.pi*30*t)).astype(np.float32)
        source = output/f'hpf_{seed}_input.wav'
        write_float(source, rumble)
        for hz in (40,60,80):
            dest = output/f'hpf_{seed}_{hz}.wav'
            ffmpeg.run(['-i',str(source),'-af',f'highpass=f={hz}:p=2','-c:a','pcm_f32le',str(dest)])
            decoded = ffmpeg.decode_f32(dest,rate=48000)
            def band(x,lo,hi):
                spectrum=np.abs(np.fft.rfft(x))**2
                f=np.fft.rfftfreq(len(x),1/48000)
                return float(spectrum[(f>=lo)&(f<=hi)].sum())
            results['highpass'].append({'seed':seed,'hz':hz,'metrics':metrics(clean,decoded,mask,48000),
                'rumble_change_db':float(10*np.log10((band(decoded,20,40)+1e-20)/(band(rumble,20,40)+1e-20))),
                'fundamental_change_db':float(10*np.log10((band(decoded,90,150)+1e-20)/(band(clean,90,150)+1e-20)))})
        for anchor in ('uniform','vad'):
            for fit in ('theil_sen','ransac'):
                speaker,reference = make_pair(.35,20,seed=seed,dur=120)
                for case in ('normal','silence','unrelated','jump'):
                    own, ref = speaker.copy(), reference.copy()
                    if case=='silence':
                        own[30*8000:70*8000]=0
                    elif case=='unrelated':
                        own=np.random.default_rng(seed+500).normal(0,.01,len(own)).astype(np.float32)
                    elif case=='jump':
                        own[len(own)//2:]=np.roll(own[len(own)//2:],80)
                    started=time.perf_counter()
                    try:
                        est=analysis.estimate_sync(own,ref,n_segments=8,segment_s=8,anchor_mode=anchor,fit_method=fit)
                        row={'accepted':est.quality['safe'],'offset_error_ms':(est.offset_s-.35)*1000,
                             'ppm_error':est.drift_ppm-20,'quality':est.quality}
                    except ValueError as exc:
                        row={'accepted':False,'error':str(exc)}
                    results['sync'].append({'seed':seed,'anchor':anchor,'fit':fit,'case':case,
                                            'seconds':time.perf_counter()-started,**row})
        for contamination in (0, .25, .5, .7):
            times=np.arange(16)*30.0
            lag=1.234+times*12e-6
            rng=np.random.default_rng(seed)
            bad=rng.choice(16,round(16*contamination),replace=False)
            lag[bad]+=rng.uniform(.03,.2,len(bad))
            for method in ('theil_sen','ransac'):
                try:
                    slope,origin,keep=fit_clock(times,lag,np.ones(16),np.ones(16,dtype=bool),method)
                    row={'offset_error_ms':(origin-1.234)*1000,'ppm_error':slope*1e6-12,'inliers':int(keep.sum())}
                except ValueError as exc:
                    row={'error':str(exc)}
                results['fits'].append({'seed':seed,'outlier_fraction':contamination,'method':method,**row})
        clean8,_=synthetic_speech(seed,sr=8000,duration=12)
        delay=np.concatenate([np.zeros(32),clean8[:-32]])*-.3
        independent=np.random.default_rng(seed).normal(0,.01,len(clean8))
        results['bleed'].append({'seed':seed,'known_delay_inverted':bleed_pairs({'a':clean8,'b':delay}),
                                 'independent':bleed_pairs({'a':clean8,'b':independent})})
    # Measure independent, known clock distortion and real correction round trips.
    # Long enough to resolve ppm; create timing error independently by interpolation.
    for ppm in (-100,-12,12,100):
        speaker,reference=make_pair(.35,ppm,seed=71,dur=120)
        source=output/f'clock_{ppm}_input.wav'
        write_float(source,speaker,8000)
        for method in ('rubberband','polyphase'):
            dest=output/f'clock_{ppm}_{method}.wav'
            filters=correction_filters(.35,ppm,len(reference),5,8000,method)
            elapsed, rss = measured_ffmpeg(['-i',str(source),'-af',','.join(filters),'-c:a','pcm_f32le',str(dest)])
            corrected=ffmpeg.decode_f32(dest,rate=8000)
            est=analysis.estimate_sync(corrected,reference,n_segments=8,segment_s=8)
            tone=output/f'tone_{ppm}.wav'
            tone_out=output/f'tone_{ppm}_{method}.wav'
            samples=np.sin(2*np.pi*440*np.arange(80000)/8000).astype(np.float32)*.3
            write_float(tone,samples,8000)
            ffmpeg.run(['-i',str(tone),'-af',','.join(correction_filters(0,ppm,len(samples),5,8000,method)),
                        '-c:a','pcm_f32le',str(tone_out)])
            y=ffmpeg.decode_f32(tone_out,rate=8000)[8000:-8000]
            # Phase increment at a complex-demodulated carrier gives finer than FFT-bin resolution.
            block=800
            phase=[]
            for i in range(0,len(y)-block,block):
                z=np.dot(y[i:i+block],np.exp(-2j*np.pi*440*np.arange(i,i+block)/8000))
                phase.append(np.angle(z))
            df=float(np.polyfit(np.arange(len(phase))*.1,np.unwrap(phase),1)[0]/(2*np.pi))
            results['resampling'].append({'ppm':ppm,'method':method,'seconds':elapsed,'rss_kib_sampled':rss,'samples':len(corrected),
                'residual_offset_ms':est.offset_s*1000,'residual_ppm':est.drift_ppm,
                'max_segment_ms':max(abs(s['lag_ms']) for s in est.segments if s['used']),
                'pitch_change_cents':float(1200*np.log2((440+df)/440))})
    (output/'report.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seeds',type=int,nargs='+',default=[101,107,129])
    args=parser.parse_args()
    result=run(args.output,args.seeds)
    print({k:len(v) for k,v in result.items() if isinstance(v,list)})
