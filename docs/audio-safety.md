# Audio pipeline improvement: stage 1

This PR establishes a reproducible A/B baseline and bounds Cleanup's linear
gain. It does not change the denoiser, gate, high-pass default, sync estimator,
Rubber Band correction, stage order, or A/B playback controls.

## Behavior

Cleanup keeps pre-gain audio as float32 WAV (RF64 when needed), measures
integrated loudness and True Peak using FFmpeg, then applies only linear gain
before encoding FLAC24. This prevents the old fixed-point intermediate from
clipping before its level can be reduced. The output is metered again; a peak
ceiling violation fails the stage instead of silently continuing.

Settings (editable in the existing Cleanup panel):

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_gain_db` | 12 dB | Upper bound on amplification, not attenuation |
| `track_max_gain_db` | `{}` | Optional speaker_a/b/c overrides; blank UI field inherits common cap; zero disables amplification |
| `silence_gate_lufs` | -60 LUFS | Do not amplify at/below this measured loudness |
| `peak_ceiling_dbtp` | -1 dBTP | Linear gain must leave this True Peak headroom, plus a 0.1 dB meter/quantization margin |

Safety takes priority over the LUFS target. The report includes requested/applied
gain, limiting reasons, effective settings, before/after loudness and True Peak.
The ceiling applies to Cleanup, not subsequent Dynamics/Mix; Master retains its
existing limiting. Missing/invalid peak on measurable audio stops processing.
Silence's `-inf` peak becomes JSON `null`.

Existing project JSON, approval status and fingerprints are not migrated.
Existing approved artifacts are untouched. Rerunning Cleanup uses the safety
defaults if fields are missing, including old projects that read Sync directly.
Changing a setting uses the existing downstream stale/rebuild behavior.
No runtime dependency was added. Ruff and mypy are development dependencies.

The float intermediate uses about 192 kB/s for mono 48 kHz audio (~691 MB per
hour per active track); it is deleted after the gain pass. Existing runner
failure cleanup removes partial stage files. Full metering adds two FFmpeg
read passes compared with the previous integrated measurement. This is a
safety/observability tradeoff, not a performance improvement.

## Reproduce the A/B comparison

```sh
uv sync
uv run python -m benchmarks.audio_ab --output docs/private/audio-ab
uv run pytest -q
uv run ruff check app tests benchmarks
uv run mypy app tests/audio_fixtures.py benchmarks
cd web
npm ci
npm run build
CHROME_BIN=/path/to/chrome npm run test:trim
```

The A implementation freezes Cleanup's processing order and gain behavior from
`4551fe76c243bc0260713ce1d0f5185d75c90d17`, including the FLAC intermediate and
its pre-encoding ebur128 measurement. The B implementation calls the production
Cleanup processor. Both use the same source FLAC, NR/gate/HPF settings and
FFmpeg binary. Previews are excluded from this benchmark only. The output
contains A/B FLACs, downward-only loudness-matched WAVs, and a JSON report with
seed, configuration, tool version, meter results and signal metrics.

All fixtures are original, deterministic synthesis with known voiced/unvoiced
components, a fixed speech mask and explicitly added noise. No downloaded
recordings, models or edit-radio code are included.

Measured on FFmpeg 7.1.5-0+deb13u1, seed 17, 48 kHz, six-second scenes:

| Scene | A LUFS / dBTP | B LUFS / dBTP | A gain -> B gain |
| --- | --- | --- | --- |
| Clean speech-like signal | -20.0 / -3.3 | -20.0 / -3.3 | +1.7 -> +1.7 dB |
| Stationary noise, 10 dB input SNR | -20.0 / -2.2 | -20.0 / -2.2 | +2.0 -> +2.0 dB |
| Stationary noise, 30 dB input SNR | -20.0 / -3.1 | -20.0 / -3.1 | +1.9 -> +1.9 dB |
| Quiet signal (0.005 times amplitude) | -21.1 / -5.2 | -67.1 / -52.3 | +47.1 -> 0.0 dB |
| Quiet signal with loud transient | -30.5 / +2.1 | -41.2 / -1.1 | +19.7 -> -1.5 dB |
| Silence | -70.0 / no peak | -70.0 / no peak | +50.0 -> 0.0 dB |

The transient case has 41 samples on the 24-bit rails in A and zero in B.
The quiet case deliberately stays below target: silence protection took priority.
A separate real-FFmpeg regression drives the pre-gain signal above 0 dBFS and
checks that gain-only processing preserves the signal (SI-SDR >100 dB), without
clipping, NaN/Inf, or a sample-count change. Both current Trim and legacy Sync
input paths are exercised.

These results demonstrate bounded gain and peak safety, not a perceptual quality
improvement. The harness reports a fixed-mask SI-SDR and 300–3400 Hz energy
change. SI-SDR is sensitive to HPF phase and NR latency; absolute scores on the
full chain are not an intelligibility score. Band energy includes noise and
cannot by itself prove speech preservation. Short synthesis does not establish
real-recording quality or long-session performance. No listening preference is
claimed. Future NR experiments must isolate NR, account for latency, use
multiple held-out seeds, test non-stationary noise and validate with permitted
real recordings before changing defaults.

## Next stages

1. Add shared VAD/noise analysis and optional NR selection; measure speech and
   non-speech coverage and fail conservatively when evidence is insufficient.
2. Evaluate non-stationarity and NR-before/after QC using a fixed evaluation mask;
   include the gate separately. Compare 40/60/80 Hz HPF without changing the
   current 80 Hz default just on theoretical grounds.
3. Evaluate VAD-selected time-distributed sync anchors and confidence safeguards;
   compare RANSAC with the existing Theil–Sen/WLS and retain 1 ms/1 ppm tests.
4. Add mic-bleed warnings; experiment with band protection and Polyphase only
   after measurable benefit, resource costs and licensing are established.

These items are not implemented by this first-stage PR.

## Licensing

podalign remains AGPL-3.0-or-later; its existing FFmpeg/librubberband NOTICE is
retained. edit-radio's inspected pyproject declares MIT, but no standalone
license text/copyright notice was present in its inspected tree. This change
uses original code and original synthetic fixtures; no source is copied from
edit-radio. Confirm the complete grant and attribution before any future reuse.
