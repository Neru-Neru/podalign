"""Stage 5 — Mix。

1. 2話者を L/R、3話者を L/C/R に薄くパン(定パワー則)→ amix normalize=0 で話者バス
   (合算ピーク +9.5dB に備え premix_gain_db を先に引く — §6.1)
2. BGM: 単体でシームレスなループ単位を作り -stream_loop -1 で必要長へ
   (aloop は 1.3GB RAM を食うため使わない — §7)
3. 話者バスをキーに sidechaincompress で BGM をダッキング
4. ジングル・話者・BGM を同じ絶対時刻のタイムラインへ配置して amix
"""
from __future__ import annotations

import math

from . import ffmpeg
from .base import StageContext

PAN_POSITIONS = {"speaker_a": -1.0, "speaker_b": 0.0, "speaker_c": 1.0}


def _non_negative_seconds(value: object, name: str) -> float:
    """有限かつ 0 以上の秒数だけを受け入れる。bool は数値として扱わない。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} は有限の 0 以上の秒数で指定してください")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} は有限の 0 以上の秒数で指定してください")
    return result


def resolve_timing(params: dict, jingle_duration_s: float, voice_duration_s: float) -> dict[str, float | None]:
    """Mix の開始指定を検証し、追従する自動開始を絶対時刻へ解決する。

    BGM は話者バスと同じ長さだけ鳴らす。開始位置を遅らせても末尾の音楽を
    切らないため、BGM の終了が番組全長を決めることがある。
    """
    jingle_duration_s = _non_negative_seconds(jingle_duration_s, "ジングル長")
    voice_duration_s = _non_negative_seconds(voice_duration_s, "話者バス長")
    jingle_start = _non_negative_seconds(params.get("jingle_start_s", 0.0), "jingle_start_s")
    raw_voice_start = params.get("voice_start_s")
    voice_start = (
        jingle_start + jingle_duration_s
        if raw_voice_start is None
        else _non_negative_seconds(raw_voice_start, "voice_start_s")
    )
    raw_bgm_start = params.get("bgm_start_s")
    bgm_start = (
        voice_start + 10.0
        if raw_bgm_start is None
        else _non_negative_seconds(raw_bgm_start, "bgm_start_s")
    )
    bgm_fade_in = _non_negative_seconds(params.get("bgm_fade_in_s", 2.0), "bgm_fade_in_s")
    jingle_end = jingle_start + jingle_duration_s
    voice_end = voice_start + voice_duration_s
    bgm_end = bgm_start + voice_duration_s
    return {
        "jingle_start_s": jingle_start,
        "voice_start_s": voice_start,
        "bgm_start_s": bgm_start,
        "bgm_fade_in_s": bgm_fade_in,
        "jingle_end_s": jingle_end,
        "voice_end_s": voice_end,
        "bgm_end_s": bgm_end,
        "program_length_s": max(jingle_end, voice_end, bgm_end),
    }


def _delay_samples(seconds: float) -> str:
    """48kHz サンプル単位の adelay 指定。小数ms丸めを避ける。"""
    return f"adelay={math.floor(seconds * 48000 + 0.5)}S:all=1"


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
    for i, role in enumerate(ctx.speakers):
        inputs += ["-i", str(dyn / f"{role}.flac")]
        position = (-1.0 if i == 0 else 1.0) if len(ctx.speakers) == 2 else PAN_POSITIONS[role]
        gl, gr = pan_gains(position, width)
        chains.append(
            f"[{i}:a]volume={premix}dB,pan=stereo|c0={gl:.4f}*c0|c1={gr:.4f}*c0[s{i}]"
        )
    bus = ctx.out_dir / "_bus.flac"
    graph = (
        ";".join(chains)
        + ";" + "".join(f"[s{i}]" for i in range(len(ctx.speakers)))
        + f"amix=inputs={len(ctx.speakers)}:normalize=0,asplit[keep][meter]"
        + ";[meter]ebur128=peak=none[m]"
    )
    stderr = ffmpeg.run([
        *inputs, "-filter_complex", graph,
        "-map", "[keep]", *ffmpeg.FLAC24, str(bus),
        "-map", "[m]", "-f", "null", "-",
    ])
    bus_i = _extract_i(stderr)
    voice_len = ffmpeg.probe(bus)["duration"]
    jingle = ingest / "jingle.flac"
    timing = resolve_timing(params, ffmpeg.probe(jingle)["duration"], voice_len)
    jingle_start = float(timing["jingle_start_s"])
    voice_start = float(timing["voice_start_s"])
    bgm_start = float(timing["bgm_start_s"])
    bgm_fade_in = float(timing["bgm_fade_in_s"])
    bgm_play_len = voice_len

    # --- 2. BGM ループ単位(継ぎ目のプチノイズ防止 — §7) ---
    ctx.progress("mix: BGM ループ単位生成")
    bgm_src = ingest / "bgm.flac"
    bgm_dur = ffmpeg.probe(bgm_src)["duration"]
    xf = min(float(params["bgm_loop_crossfade_s"]), bgm_dur / 4)
    unit = ctx.out_dir / "_bgm_unit.flac"
    if bgm_dur < bgm_play_len:
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
    jingle_gain = bus_i - ffmpeg.measure_ebur128(jingle)["I"]

    # --- 3+4. ダッキング + 絶対時刻のタイムライン合成 ---
    ctx.progress("mix: ダッキング + タイムライン合成")
    duck = (
        f"sidechaincompress=threshold={float(params['duck_threshold_db'])}dB"
        f":ratio={float(params['duck_ratio'])}:attack=20:release=300"
    )
    fade_out = max(0.0, bgm_play_len - 3.0)
    fade_out_duration = min(3.0, bgm_play_len)
    # キー音声のEOFでダッキングが終了しないよう、BGM末尾まで無音を補う。
    key_samples = math.ceil(float(timing["program_length_s"]) * 48000)
    bgm_filters = (
        f"atrim=0:{bgm_play_len:.6f},asetpts=PTS-STARTPTS,"
        f"volume={bgm_gain:.2f}dB"
    )
    if bgm_fade_in > 0:
        bgm_filters += f",afade=t=in:st=0:d={bgm_fade_in:.6f}"
    if fade_out_duration > 0:
        bgm_filters += f",afade=t=out:st={fade_out:.6f}:d={fade_out_duration:.6f}"
    graph = (
        # sidechaincompress はチャンネルレイアウト未確定を許さないため aformat を明示。
        # 各入力を無音で先頭埋めして、番組先頭を共通の t=0 に固定する。
        f"[0:a]volume={jingle_gain:.2f}dB,{_delay_samples(jingle_start)}[j];"
        f"[1:a]aformat=channel_layouts=stereo,asplit[busmix][buskey0];"
        f"[busmix]{_delay_samples(voice_start)}[voice];"
        f"[buskey0]{_delay_samples(voice_start)},apad=whole_len={key_samples}[buskey];"
        f"[2:a]{bgm_filters},aformat=channel_layouts=stereo,"
        f"{_delay_samples(bgm_start)}[bgm];"
        f"[bgm][buskey]{duck}[duck];"
        f"[j][voice][duck]amix=inputs=3:duration=longest:normalize=0[out]"
    )
    ffmpeg.run([
        "-i", str(jingle),
        "-i", str(bus),
        # 入力側 -t で読み込みを打ち切る: -stream_loop -1 の無限入力を
        # atrim だけで切るとフィルタが EOF を受け取れず終了しない
        "-stream_loop", "-1", "-t", f"{bgm_play_len + 1:.3f}", "-i", str(unit),
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
        "bgm_looped": bgm_dur < bgm_play_len,
        "jingle_start_s": round(jingle_start, 3),
        "voice_start_s": round(voice_start, 3),
        "bgm_start_s": round(bgm_start, 3),
        "bgm_fade_in_s": round(bgm_fade_in, 3),
        "jingle_end_s": round(float(timing["jingle_end_s"]), 3),
        "voice_end_s": round(float(timing["voice_end_s"]), 3),
        "bgm_end_s": round(float(timing["bgm_end_s"]), 3),
        "program_length_s": round(float(timing["program_length_s"]), 3),
        "mix_loudness": final,
        "warnings": [],
    }
