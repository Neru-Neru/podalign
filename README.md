# podalign

[![CI](https://github.com/Neru-Neru/podalign/actions/workflows/ci.yml/badge.svg)](https://github.com/Neru-Neru/podalign/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

**±1 ms offset / ±1 ppm drift auto-sync for double-ender podcast recordings** —
plus a full, hands-off mastering pipeline (noise reduction → compression → mix →
-16 LUFS loudness) you approve stage by stage in a web UI.

[日本語版 README はこちら / Japanese README](README.ja.md)

> **Status: early release.** Verified extensively against synthetic test material;
> validation on real-world recordings is in progress. Expect rough edges and
> please report what breaks.

## The problem

In a double-ender (each host records locally, e.g. over a video call), the local
tracks don't just start at different times — every device's crystal clock is off
by ±20–50 ppm, which accumulates to **up to ~180 ms of drift per hour**. Nudging
tracks by hand fixes the start offset but not the drift; by the end of the
episode the crosstalk is audibly out of sync.

podalign estimates **both the fixed offset and the clock-drift rate** of each
local track against a reference recording (the call recording with everyone's
voice), corrects them, then re-correlates to verify the residual error is
within ±1 ms — and warns you in the UI if it isn't.

### How the sync works

1. **Coarse search** — cross-correlate 50 Hz RMS envelopes → ±20 ms accuracy
2. **Piecewise refinement** — split the recording into ~10 windows, correlate
   8 kHz waveforms with GCC-PHAT (tapered, parabolic interpolation) →
   a sub-sample-accurate "time vs. lag" series
3. **Robust fit** — drop windows where the speaker is silent (RMS + correlation
   confidence), Theil–Sen initial estimate + weighted least squares →
   intercept = offset, slope = drift rate
4. **Correction** — sample-accurate `atrim`/`adelay` for the offset;
   `rubberband=tempo=(1+s)` for drift when |s| ≥ 5 ppm
   (`atempo` is a no-op at ppm scale — measured, not assumed)
5. **Verification** — re-correlate after correction; assert residual ≤ ±1 ms

## The rest of the pipeline

All audio processing is done by ffmpeg subprocesses; Python (numpy) only
analyzes and picks parameters. Seven deterministic stages, each a pure function
of its upstream artifacts + parameters — change a parameter and only the stale
downstream stages re-run. You listen to and approve each stage in the web UI.

| Stage | What it does |
|---|---|
| 0 Ingest | ffprobe validation → unify to 48 kHz / FLAC 24-bit → clipping / DC / silence QC |
| 1 Sync | offset + clock-drift estimation and correction (above) |
| 2 Cleanup | high-pass 80 Hz → auto hum notch (50/60 Hz detection) → declick/declip → `afftdn` NR (≤12 dB) → de-esser → gentle gate → linear gain to -20 LUFS |
| 3 Dynamics | two-stage compression (glue 2.5:1 → peak 6:1) + subtle EQ |
| 4 Mix | L/C/R micro-panning → `amix` → seamlessly looped BGM with sidechain ducking → jingle crossfade |
| 5 Master | two-pass linear `loudnorm` to **-16 LUFS / -1.5 dBTP** → safety limiter -1.0 dB |
| 6 Export | WAV 24-bit / MP3 192k CBR / AAC 128k + QC report |

**Input (6 files):** three local speaker tracks, one reference (call) recording
used only for sync, one jingle, one loopable BGM. The 3-speaker layout is
currently fixed.

## Quick start

### Docker (recommended)

```bash
docker build -t podalign .
docker run -p 8000:8000 -v $(pwd)/data:/data podalign
# open http://localhost:8000
```

The image bundles an ffmpeg build with librubberband enabled — the one
dependency that trips people up (see [License](#license)).

### Manual

Requirements: **ffmpeg with librubberband enabled** (Debian/Ubuntu `apt install
ffmpeg` and Homebrew builds qualify), Python 3.10+ with
[`uv`](https://docs.astral.sh/uv/), Node 22 (UI build only).

```bash
uv sync
cd web && npm install && npm run build && cd ..
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

podalign checks for librubberband at startup and refuses to start with a clear
message if your ffmpeg lacks it.

## ⚠️ Security model: single user, private network only

podalign has **no authentication and no multi-tenancy**. It is designed to run
on your own machine, a home server on your LAN, or behind a VPN such as
Tailscale. **Do not expose it directly to the internet.** Anyone who can reach
the port can upload audio, run jobs, and delete projects.

## Storage & re-runs

Each stage's input fingerprint is defined recursively as
`hash(upstream fingerprints + parameters)`, so results are reproducible and
intermediate FLACs are just a cache: the UI can garbage-collect them
(~3.5 GB per episode target) and any missing intermediate is regenerated
automatically from upstream.

## Tests

```bash
uv run pytest -q                                      # all, incl. E2E (~2 min)
uv run pytest -q --ignore=tests/test_pipeline_e2e.py  # fast tests only
```

- `tests/test_sync.py` — recovers **offset within ±1 ms / drift within ±1 ppm**
  on synthetic tracks; pins the correction sign through a rubberband round-trip
- `tests/test_pipeline_e2e.py` — full pipeline on 6 synthetic inputs, asserts
  measured **-16 ± 0.5 LUFS / ≤ -1.0 dBTP**, verifies GC → regeneration

## Design docs

Detailed design notes (currently in Japanese): [docs/design.md](docs/design.md),
[docs/design-review.md](docs/design-review.md) — including the measurements
behind "atempo doesn't work at ppm scale" and the GCC-PHAT pitfalls.

## License

podalign is licensed under **AGPL-3.0** (see [LICENSE](LICENSE)). Self-hosting
for yourself or your show has no practical obligations; the AGPL matters only
if you offer podalign as a network service to others.

The Docker image additionally bundles **ffmpeg with librubberband (GPL)**;
see [NOTICE](NOTICE) for the required attribution and source availability.
