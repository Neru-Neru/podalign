"""Stage 4 — Dynamics(コンプ・EQ)。

2段コンプ(グルー→ピーク)は放送・Podcast の定番手法(§7)。
EQ は既定で控えめ(3–5kHz +2dB 明瞭度 / 200–300Hz -2dB こもり除去)。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import ffmpeg
from .base import StageContext


def build_chain(params: dict) -> str:
    c1, c2 = params["comp1"], params["comp2"]
    filters = [
        (f"acompressor=threshold={c1['threshold_db']}dB:ratio={c1['ratio']}"
         f":attack={c1['attack_ms']}:release={c1['release_ms']}"),
        (f"acompressor=threshold={c2['threshold_db']}dB:ratio={c2['ratio']}"
         f":attack={c2['attack_ms']}:release={c2['release_ms']}"),
    ]
    if params.get("eq_enabled", True):
        filters.append(
            f"equalizer=f=4000:width_type=o:width=1.5:g={float(params['eq_presence_db'])}"
        )
        filters.append(
            f"equalizer=f=250:width_type=o:width=1:g={float(params['eq_mud_db'])}"
        )
    return ",".join(filters)


def _process(ctx: StageContext, role: str, chain: str) -> dict:
    src = ctx.upstream_dir("cleanup") / f"{role}.flac"
    out = ctx.out_dir / f"{role}.flac"
    ctx.progress(f"dynamics: {role}")
    ffmpeg.run(["-i", str(src), "-af", chain, *ffmpeg.FLAC24, str(out)])
    measured = ffmpeg.measure_ebur128(out)
    ctx.make_preview(out, role)
    ctx.make_peaks(out, role)
    return {"loudness": measured}


def run(ctx: StageContext, params: dict) -> dict:
    chain = build_chain(params)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(zip(ctx.speakers, pool.map(lambda r: _process(ctx, r, chain), ctx.speakers)))
    return {"tracks": results, "filter_chain": chain, "warnings": []}
