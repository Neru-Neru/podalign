"""Stage 5 — Master。

loudnorm は必ず2パス(1パスのダイナミックモードは音を潰す — §7)。
alimiter はサンプルピークの安全網で、真の TP 制御は loudnorm の TP=-1.5 が担う(R-2)。
loudnorm は内部 192kHz で動くため後段で 48kHz へ戻す。
"""
from __future__ import annotations

from . import ffmpeg
from .base import StageContext


def run(ctx: StageContext, params: dict) -> dict:
    src = ctx.upstream_dir("mix") / "mix.flac"
    out = ctx.out_dir / "master.flac"
    i, tp, lra = float(params["target_i"]), float(params["target_tp"]), float(params["target_lra"])

    ctx.progress("master: loudnorm 1パス目(測定)")
    measured = ffmpeg.measure_loudnorm(src, i=i, tp=tp, lra=lra)

    ctx.progress("master: loudnorm 2パス目(linear) + リミッター")
    loudnorm = (
        f"loudnorm=I={i}:TP={tp}:LRA={lra}:linear=true"
        f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
    )
    # level=0 必須: alimiter 既定の auto-level はリミット後に 1/limit (+1.0dB) の
    # ゲインを掛けてラウドネス目標を壊す(実測で確認)
    chain = (
        f"{loudnorm},aresample=48000,"
        "alimiter=limit=-1dB:attack=5:release=50:asc=1:level=0"
    )
    ffmpeg.run(["-i", str(src), "-af", chain, *ffmpeg.FLAC24, str(out)])

    ctx.progress("master: 最終測定")
    final = ffmpeg.measure_ebur128(out)
    warnings = []
    if final["I"] is not None and abs(final["I"] - i) > 0.5:
        warnings.append(
            f"Integrated {final['I']} LUFS が目標 {i}±0.5 を外れています"
            "(linear ゲインが TP 制約で頭打ちの可能性)"
        )
    if final["TP"] is not None and final["TP"] > -1.0:
        warnings.append(f"True Peak {final['TP']} dBTP が -1.0 を超えています")

    ctx.make_preview(out, "master")
    ctx.make_peaks(out, "master")
    return {
        "measured_pass1": {k: measured[k] for k in
                           ("input_i", "input_tp", "input_lra", "input_thresh")},
        "normalization_type": measured.get("normalization_type"),
        "final": final,
        "target": {"I": i, "TP": tp, "LRA": lra},
        "warnings": warnings,
    }
