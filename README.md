# podalign

[![CI](https://github.com/Neru-Neru/podalign/actions/workflows/ci.yml/badge.svg)](https://github.com/Neru-Neru/podalign/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

**Turns separately recorded podcast tracks into a publish-ready episode, automatically.**
Runs on your own computer (or home server); you operate it from your browser.

[日本語版 README はこちら / Japanese README](README.ja.md)

> **Status: early release.** Verified extensively against synthetic test material;
> validation on real-world recordings is in progress. Expect rough edges and
> please report what breaks.

## Browser-only static app

The repository now includes a backend-free browser build. It is served from `web/dist` and can run on GitHub Pages without FastAPI, ffmpeg, an API, or an upload server.

```bash
cd web
npm ci
npm run build
npm run preview
```

Open the printed URL, select the six audio files, and click **ブラウザで処理**. Audio stays in the browser. Decode and processing run in a module Web Worker; the final WAV is returned as a downloadable Blob. Set GitHub Pages to deploy the `web/dist` artifact from Actions (the build accepts `VITE_BASE_PATH=/<repo-name>/`).

### Architecture migration

| Current server edition | Browser edition |
|---|---|
| React → FastAPI JSON/chunk upload | React → Web Audio `File` decode |
| Python/NumPy GCC-PHAT + ffmpeg stages | Worker-side offset search, drift estimate, cleanup, mix, ducking, master and QC |
| FLAC and preview artifacts on server disk | Transferable `Float32Array` buffers and one final WAV Blob |
| ffmpeg/librubberband, Python, Docker | Static HTML/JS/CSS; no runtime server |

The Worker processes speaker tracks sequentially and transfers channel buffers rather than cloning them. It reports progress, supports cancellation, and does not retain intermediate stage files. This is intentionally a maintainable browser-native baseline: exact parity with the server's GCC-PHAT/rubberband and true streaming decode require WebCodecs/ffmpeg.wasm and are listed as remaining limitations in the UI and PR.

## What it does

When you record remotely (talking over a video call while everyone records
locally), you run into two problems once you stack the tracks:

- Each recording starts at a different moment
- Worse, every device's internal clock runs at a slightly different speed, so
  **the tracks drift apart over time** — up to about 0.2 seconds per hour

Lining up the beginnings by hand fixes the first problem but not the second.

podalign uses the call recording as a reference to automatically measure
**both the start offset and the drift rate** of each local track, corrects
them, and then double-checks that the remaining error is within a
thousandth of a second — warning you on screen if it isn't.

After that, it handles the standard podcast finishing steps for you:

- Removing noise and electrical hum
- Evening out everyone's voice level
- Mixing in your music and jingle (the music automatically ducks under speech)
- Bringing the episode to standard podcast loudness (-16 LUFS)

At every step you **listen to before/after from the same position in your
browser and approve it before moving on** — nothing changes your audio behind
your back. At the end you export finished WAV / MP3 / AAC files.

## What you need

Six audio files:

| File | Description |
|---|---|
| 3 speaker recordings | Each host's local recording (currently fixed at 3 hosts) |
| 1 call recording | The Zoom/call-side recording. **Used only to measure the drift — it never ends up in the final mix** |
| 1 jingle | A short sound for your opening, etc. |
| 1 music track | Background music that will be looped |

## Getting started

### With Docker (recommended)

If you have [Docker](https://docs.docker.com/get-docker/), two commands get you running:

```bash
docker build -t podalign .
docker run -p 8000:8000 -v $(pwd)/data:/data podalign
```

Open http://localhost:8000 in your browser and you're set.
Everything needed (including a special build of ffmpeg) is bundled — no other
setup required.

### Without Docker

You'll need:

- **ffmpeg** (a build with librubberband enabled — the ones from Ubuntu's
  `apt install ffmpeg` or Homebrew on Mac work as-is)
- **Python 3.10+** with [`uv`](https://docs.astral.sh/uv/)
- **Node 22** (only used once, to build the web UI)

```bash
# First time only: install dependencies and build the UI
uv sync
cd web && npm install && npm run build && cd ..

# Run
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 in your browser.
If your ffmpeg is missing the required feature, podalign stops at startup with
a clear error message, so you'll know right away.

## ⚠️ Keep it on a network only you can reach

podalign has **no login**. Use it on your own machine, your home LAN, or
inside a VPN such as Tailscale. **Do not expose it directly to the internet.**
Anyone who can reach the address can upload audio, run jobs, and delete
projects.

## FAQ

**Q. Will the working files fill up my disk?**
Intermediate files take roughly 3.5 GB per episode. You can delete them from
the UI, and anything deleted is regenerated automatically if it's needed
again — so it's always safe to clean up.

**Q. Can I redo a step I already approved?**
Yes. When you change a setting, only the steps affected by it are redone;
everything else stays as-is.

**Q. Does it work for 2-host or 4-host shows?**
Currently only the 3-host layout is supported.

## For the curious: how it works

<details>
<summary>The sync algorithm (technical)</summary>

1. **Coarse search** — cross-correlate 50 Hz RMS envelopes → ±20 ms accuracy
2. **Piecewise refinement** — split the recording into ~10 windows, correlate
   8 kHz waveforms with GCC-PHAT (tapered, parabolic interpolation) →
   a sub-sample-accurate "time vs. lag" series
3. **Robust fit** — drop windows where the speaker is silent (RMS +
   correlation confidence), Theil–Sen initial estimate + weighted least
   squares → intercept = offset, slope = drift rate
4. **Correction** — sample-accurate `atrim`/`adelay` for the offset;
   `rubberband=tempo=(1+s)` for drift when |s| ≥ 5 ppm
   (`atempo` is a no-op at ppm scale — measured, not assumed)
5. **Verification** — re-correlate after correction; assert residual ≤ ±1 ms

</details>

<details>
<summary>What each stage does (technical)</summary>

All audio processing is done by ffmpeg subprocesses; Python (numpy) only
analyzes and picks parameters. Each stage is a deterministic pure function of
its upstream artifacts + parameters.

| Stage | What it does |
|---|---|
| 0 Ingest | ffprobe validation → unify to 48 kHz / FLAC 24-bit → clipping / DC / silence QC |
| 1 Sync | offset + clock-drift estimation and correction |
| 2 Cleanup | high-pass 80 Hz → auto hum notch (50/60 Hz detection) → declick/declip → `afftdn` NR (≤12 dB) → de-esser → gentle gate → linear gain to -20 LUFS |
| 3 Dynamics | two-stage compression (glue 2.5:1 → peak 6:1) + subtle EQ |
| 4 Mix | L/C/R micro-panning → `amix` → seamlessly looped BGM with sidechain ducking → jingle crossfade |
| 5 Master | two-pass linear `loudnorm` to -16 LUFS / -1.5 dBTP → safety limiter -1.0 dB |
| 6 Export | WAV 24-bit / MP3 192k CBR / AAC 128k + QC report |

</details>

Full design notes, including the measurements behind each decision (currently
in Japanese):

- [docs/design.md](docs/design.md) — full design, including the "atempo
  doesn't work at ppm scale" measurements and the GCC-PHAT pitfalls
- [docs/design-review.md](docs/design-review.md) — pre-implementation design
  review record

Developer notes:

```bash
uv run pytest -q                                      # all tests, incl. E2E (~2 min)
uv run pytest -q --ignore=tests/test_pipeline_e2e.py  # fast tests only

# frontend development with hot reload
uv run uvicorn app.main:app --reload          # backend on :8000
cd web && npm run dev                          # UI on :5173 (/api proxied to 8000)
```

## License

**AGPL-3.0** (see [LICENSE](LICENSE)).
**Using podalign for yourself or your own show carries no practical
obligations.** The AGPL's source-sharing requirement only applies if you offer
podalign as a network service to others.

The Docker image additionally bundles **ffmpeg with librubberband (GPL)**;
see [NOTICE](NOTICE) for the required attribution and source availability.
