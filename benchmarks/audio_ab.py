"""Compare the frozen pre-safety Cleanup algorithm against safe Cleanup.

Run: uv run python -m benchmarks.audio_ab --output docs/private/audio-ab
Generated audio is original synthetic material. Reports and level-matched WAVs
are retained for review; temporary project data is removed. No edit-radio code.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from app import project as prj, storage
from app.pipeline import analysis, ffmpeg, s2_cleanup
from app.pipeline.base import StageContext
from tests.audio_fixtures import add_noise, metrics, synthetic_speech, write_float

class BenchmarkContext(StageContext):
    def make_preview(self, src: Path, name: str, bitrate: str = "96k") -> None:
        pass

    def make_peaks(self, src: Path, name: str) -> None:
        pass


BASELINE_COMMIT = "4551fe76c243bc0260713ce1d0f5185d75c90d17"
SR = 48000


def legacy_cleanup(source: Path, output: Path, params: dict) -> float:
    """Frozen operation order from BASELINE_COMMIT, including FLAC pre-gain."""
    x = ffmpeg.decode_f32(source, rate=8000)
    chain = s2_cleanup.build_chain(
        params, analysis.find_noise_floor(x, 8000), analysis.detect_hum(x, 8000), False,
    )
    tmp = output.with_name("legacy_pregain.flac")
    stderr = ffmpeg.run([
        "-i", str(source), "-filter_complex",
        f"[0:a]{chain},asplit[keep][meter];[meter]ebur128=peak=none[m]",
        "-map", "[keep]", *ffmpeg.FLAC24, str(tmp),
        "-map", "[m]", "-f", "null", "-",
    ])
    match = ffmpeg._EBUR_I.search(stderr[stderr.rfind("Summary:"):])
    if match is None:
        raise ValueError("No legacy loudness measurement")
    loudness = float(match.group(1))
    gain = params["target_lufs"] - loudness
    ffmpeg.run(["-i", str(tmp), "-af", f"volume={gain:.2f}dB", *ffmpeg.FLAC24, str(output)])
    tmp.unlink()
    return gain


def compare(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    clean, speech = synthetic_speech(seed=17)
    transient = clean * 0.03
    transient[SR:SR + 10] = 0.95
    scenes = {
        "clean": clean,
        "quiet": clean * 0.005,
        "stationary_10db": add_noise(clean, speech, 10, seed=18),
        "stationary_30db": add_noise(clean, speech, 30, seed=19),
        "transient": transient,
        "silence": np.zeros_like(clean),
    }
    params = dict(prj.DEFAULT_PARAMS["cleanup"])
    # Pin old processing knobs, even if future defaults change.
    params.update(highpass_hz=80, nr_max_db=12, gate_range_db=-12,
                  target_lufs=-20, deess_intensity=0.3)
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
    report: dict = {"baseline_commit": BASELINE_COMMIT, "ffmpeg": version,
                    "sample_rate": SR, "seed": 17, "params": params, "scenes": {}}
    original_data_dir = prj.DATA_DIR
    try:
        with tempfile.TemporaryDirectory(prefix="podalign-ab-") as scratch:
            prj.DATA_DIR = Path(scratch)
            for name, samples in scenes.items():
                doc = prj.create_project(name)
                doc["assets"]["speaker_a"] = {}
                ctx = BenchmarkContext(doc["id"], "cleanup", doc)
                source_dir = storage.stage_dir(doc["id"], "trim")
                source_dir.mkdir(parents=True)
                source = source_dir / "speaker_a.flac"
                write_float(source_dir / "input.wav", samples)
                ffmpeg.run(["-i", str(source_dir / "input.wav"), *ffmpeg.FLAC24, str(source)])
                legacy = output / f"{name}_A.flac"
                safe = output / f"{name}_B.flac"
                gain_a = legacy_cleanup(source, legacy, params)
                # Skip browser-only artifacts, not the processing or metering.
                result = s2_cleanup._process(ctx, "speaker_a", params)
                safe.write_bytes((ctx.out_dir / "speaker_a.flac").read_bytes())
                measured = {"A": ffmpeg.measure_ebur128(legacy), "B": ffmpeg.measure_ebur128(safe)}
                row: dict = {"loudness": measured, "gain_A_db": gain_a, "safe_report": result, "metrics": {}}
                for label, path in (("A", legacy), ("B", safe)):
                    decoded = ffmpeg.decode_f32(path, rate=SR)
                    # Silence has no meaningful speech-quality reference.
                    row["metrics"][label] = (
                        metrics(samples, decoded, speech, SR) if name != "silence"
                        else {"nonzero_samples": int(np.count_nonzero(decoded))}
                    )
                    if name != "silence":
                        target = min(measured["A"]["I"], measured["B"]["I"])
                        ffmpeg.run(["-i", str(path), "-af", f"volume={target - measured[label]['I']:.6f}dB",
                                    "-c:a", "pcm_s24le", str(output / f"{name}_{label}_matched.wav")])
                report["scenes"][name] = row
    finally:
        prj.DATA_DIR = original_data_dir
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.output)
    for name, row in report["scenes"].items():
        print(name, "A:", row["loudness"]["A"], "B:", row["loudness"]["B"],
              "gain", row["gain_A_db"], "->", row["safe_report"]["align_gain_db"])


if __name__ == "__main__":
    main()
