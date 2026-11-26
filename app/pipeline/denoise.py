"""Conservative FFmpeg NR with fixed-mask verification and bounded retries."""
from __future__ import annotations

from pathlib import Path

from . import ffmpeg
from .gain import finite_setting
from .voice import analyze_voice, nr_decision, nr_quality


def nr_graph(nr_db: float, noise_db: float, protected: bool = True, dry_delay_samples: int = 1200) -> str:
    nr_db = finite_setting(nr_db, "ノイズ除去量", 0, 12)
    noise_db = max(-80, min(-20, noise_db))
    wet = f"afftdn=nr={nr_db:.6f}:nf={noise_db:.2f}:tn=1"
    if nr_db == 0:
        return "[0:a]anull[out]"
    if not protected:
        return f"[0:a]{wet}[out]"
    # Complementary masks. Half of the original speech-critical band is kept;
    # smooth edges avoid sharp spectral discontinuities. Both branches use the
    # same FFT window and their weights add to one when NR is the identity.
    frequency = "b*sr/(2*nb)"
    weight = f"0.5*clip(({frequency}-200)/100,0,1)*clip((3800-{frequency})/400,0,1)"
    def mask(expr: str) -> str:
        return f"afftfilt=real='re*({expr})':imag='im*({expr})':win_size=1024:overlap=0.75"
    return (f"[0:a]asplit[d][w];[d]adelay={dry_delay_samples}S:all=1,{mask(weight)}[dry];"
            f"[w]{wet},{mask('1-(' + weight + ')')}[wet];"
            "[dry][wet]amix=inputs=2:normalize=0[out]")


def render_nr(source: Path, out: Path, amount: float, noise_db: float, protected: bool) -> None:
    info = ffmpeg.probe(source)
    rate = info["sample_rate"]
    samples = round(info["duration"] * rate)
    if amount:
        nr_delay = ffmpeg.filter_latency("afftdn=nr=6:nf=-80:tn=0", rate)
        fft_delay = ffmpeg.filter_latency("afftfilt=real=re:imag=im:win_size=1024:overlap=0.75", rate) if protected else 0
        delay = nr_delay + fft_delay
        graph = nr_graph(amount, noise_db, protected, nr_delay).replace("[0:a]", "[padded]", 1)
        graph = f"[0:a]apad=pad_len={delay}[padded];" + graph.replace("[out]", "[delayed]")
        graph += (f";[delayed]atrim=start_sample={delay}:end_sample={samples+delay},"
                  f"asetpts=PTS-STARTPTS,apad=whole_len={samples},atrim=end_sample={samples}[out]")
    else:
        graph = nr_graph(0, noise_db, protected)
    ffmpeg.run(["-i", str(source), "-filter_complex", graph,
                "-map", "[out]", "-c:a", "pcm_f32le", "-rf64", "auto", str(out)])


def verified_nr(source: Path, out: Path, mode: str, maximum: float, protected: bool, noise_db_override: float | None = None) -> dict:
    before = ffmpeg.decode_f32(source, rate=8000)
    voice = analyze_voice(before, 8000)
    amount, warnings = nr_decision(voice, mode, maximum)
    profile_db = voice.noise_db if noise_db_override is None else noise_db_override
    attempted = []
    quality: dict = {"valid": False, "suspected": False, "reason": "ノイズ除去を省略"}
    # Bounded retry: selected -> light -> off. No recursive processing of an
    # already-denoised file; each attempt always reads the original pre-NR file.
    for candidate in dict.fromkeys([amount, min(amount, 3.0), 0.0]):
        render_nr(source, out, candidate, profile_db, protected)
        quality = nr_quality(before, ffmpeg.decode_f32(out, rate=8000), voice) if candidate else {
            "valid": True, "suspected": False, "speech_band_loss_db": 0.0, "snr_improvement_db": 0.0}
        attempted.append({"nr_db": candidate, "quality": quality})
        if not quality["suspected"]:
            amount = candidate
            break
        warnings.append("ノイズ除去後の帯域減衰またはSNR低下を検出し、弱い処理で再試行")
    return {"requested_mode": mode, "nr_db": amount, "speech_band_protected": protected,
            "analysis": voice.report(), "noise_profile_db": profile_db, "latency_compensated": True, "quality": quality, "attempts": attempted, "warnings": warnings}
