"""Stage 5 — Mix。

1. 3話者を L/C/R に薄くパン(定パワー則)→ amix normalize=0 で話者バス
   (合算ピーク +9.5dB に備え premix_gain_db を先に引く — §6.1)
2. BGM: 単体でシームレスなループ単位を作り -stream_loop -1 で必要長へ
   (aloop は 1.3GB RAM を食うため使わない — §7)
3. 話者バスをキーに sidechaincompress で BGM をダッキング
4. ジングルを冒頭に acrossfade で接続
"""
from __future__ import annotations

import math

from . import ffmpeg
from .base import SPEAKERS, StageContext

PAN_POSITIONS = {"speaker_a": -1.0, "speaker_b": 0.0, "speaker_c": 1.0}


def pan_gains(position: float, width: float) -> tuple[float, float]:
    """定パワーパン。position∈[-1,1] × width(既定0.3 = 薄く — モノ再生で破綻させない)。"""
    p = position * width
    theta = (p + 1) * math.pi / 4
    return math.cos(theta), math.sin(theta)


def _extract_i(stderr: str) -> float:
    summary = stderr[stderr.rfind("Summary:"):]
    m = ffmpeg._EBUR_I.search(summary)
    if not m:
        raise ValueError("ebur128 Summary が見つかりません")
    return float(m.group(1))


def run(ctx: StageContext, params: dict) -> dict:
    dyn = ctx.upstream_dir("dynamics")
    ingest = ctx.upstream_dir("ingest")
    out = ctx.out_dir / "mix.flac"
    width = float(params["pan_width"])
    premix = float(params["premix_gain_db"])

    # --- 1. 話者バス(+ ebur128 同時測定) ---
    ctx.progress("mix: 話者バス合成")
    inputs, chains = [], []
    for i, role in enumerate(SPEAKERS):
        inputs += ["-i", str(dyn / f"{role}.flac")]
        gl, gr = pan_gains(PAN_POSITIONS[role], width)
        chains.append(
            f"[{i}:a]volume={premix}dB,pan=stereo|c0={gl:.4f}*c0|c1={gr:.4f}*c0[s{i}]"
        )
    bus = ctx.out_dir / "_bus.flac"
    graph = (
        ";".join(chains)
        + f";[s0][s1][s2]amix=inputs=3:normalize=0,asplit[keep][meter]"
        + ";[meter]ebur128=peak=none[m]"
    )
    stderr = ffmpeg.run([
        *inputs, "-filter_complex", graph,
        "-map", "[keep]", *ffmpeg.FLAC24, str(bus),
        "-map", "[m]", "-f", "null", "-",
    ])
    bus_i = _extract_i(stderr)
    program_len = ffmpeg.probe(bus)["duration"]

    # --- 2. BGM ループ単位(継ぎ目のプチノイズ防止 — §7) ---
    ctx.progress("mix: BGM ループ単位生成")
    bgm_src = ingest / "bgm.flac"
    bgm_dur = ffmpeg.probe(bgm_src)["duration"]
    xf = min(float(params["bgm_loop_crossfade_s"]), bgm_dur / 4)
    unit = ctx.out_dir / "_bgm_unit.flac"
    if bgm_dur < program_len:
        # unit = acrossfade(bgm, bgm)[xf:D] → 末尾→先頭が既にクロスフェード済み
        ffmpeg.run([
            "-i", str(bgm_src), "-i", str(bgm_src),
            "-filter_complex",
            f"[0:a][1:a]acrossfade=d={xf:.3f},atrim=start={xf:.3f}:end={bgm_dur:.3f},"
            "asetpts=PTS-STARTPTS[u]",
            "-map", "[u]", *ffmpeg.FLAC24, str(unit),
        ])
    else:
        ffmpeg.run(["-i", str(bgm_src), *ffmpeg.FLAC24, str(unit)])
    unit_i = ffmpeg.measure_ebur128(unit)["I"]

    # BGM ベースレベル: 話者バス実測 I に対し bgm_bed_db(既定 -24)下
    bgm_gain = (bus_i + float(params["bgm_bed_db"])) - unit_i

    # ジングルは話者バスとラウドネスを揃える
    jingle = ingest / "jingle.flac"
    jingle_gain = bus_i - ffmpeg.measure_ebur128(jingle)["I"]

    # --- 3+4. ダッキング + ジングル接続 ---
    ctx.progress("mix: ダッキング + ジングル接続")
    duck = (
        f"sidechaincompress=threshold={float(params['duck_threshold_db'])}dB"
        f":ratio={float(params['duck_ratio'])}:attack=20:release=300"
    )
    jxf = float(params["jingle_crossfade_s"])
    fade_out = max(0.0, program_len - 3.0)
    graph = (
        # sidechaincompress はチャンネルレイアウト未確定を許さないため aformat を明示
        f"[1:a]aformat=channel_layouts=stereo,asplit[bus1][buskey];"
        f"[2:a]atrim=0:{program_len:.3f},asetpts=PTS-STARTPTS,"
        f"volume={bgm_gain:.2f}dB,afade=t=out:st={fade_out:.3f}:d=3,"
        f"aformat=channel_layouts=stereo[bgm];"
        f"[bgm][buskey]{duck}[duck];"
        f"[bus1][duck]amix=inputs=2:normalize=0[program];"
        f"[0:a]volume={jingle_gain:.2f}dB[j];"
        f"[j][program]acrossfade=d={jxf:.3f}[out]"
    )
    ffmpeg.run([
        "-i", str(jingle),
        "-i", str(bus),
        # 入力側 -t で読み込みを打ち切る: -stream_loop -1 の無限入力を
        # atrim だけで切るとフィルタが EOF を受け取れず終了しない
        "-stream_loop", "-1", "-t", f"{program_len + 1:.3f}", "-i", str(unit),
        "-filter_complex", graph,
        "-map", "[out]", *ffmpeg.FLAC24, str(out),
    ])
    unit.unlink()
    bus.unlink()

    final = ffmpeg.measure_ebur128(out)
    ctx.make_preview(out, "mix")
    ctx.make_peaks(out, "mix")
    return {
        "bus_lufs": bus_i,
        "bgm_gain_db": round(bgm_gain, 2),
        "jingle_gain_db": round(jingle_gain, 2),
        "bgm_looped": bgm_dur < program_len,
        "program_length_s": round(program_len, 3),
        "mix_loudness": final,
        "warnings": [],
    }
