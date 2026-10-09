"""Stage 3 — Cleanup(話者ごとの整音)。順序が音質を決める(§7)。

1. ハイパス 80Hz(DC も落ちる) 2. ハム自動検出→ノッチ 3. declick/declip(QC 検出時)
4. afftdn(ノイズフロア自動推定、削減量 12dB クランプ) 5. ディエッサー
6. ノイズゲート(range で -12dB に留める)
7. 話者間ラウドネス整合: ebur128 測定 + volume の純線形ゲインで -20 LUFS(R-5)
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import analysis, ffmpeg
from .base import SPEAKERS, PREVIEW_BITRATE_CLEANUP, StageContext


def build_chain(
    params: dict, noise_floor_db: float, hum_hz: int | None, needs_declip: bool
) -> str:
    nr_db = min(float(params["nr_max_db"]), 12.0)   # かけすぎ=水中音が最大の失敗モード
    nf = max(-80.0, min(-20.0, noise_floor_db))
    gate_thr = max(-90.0, nf + 6.0)
    filters = [
        f"highpass=f={int(params['highpass_hz'])}:p=2",
    ]
    if hum_hz:
        # 基本波と倍音3本をノッチ(日本は東西で 50/60Hz が違うため自動検出 — §7)
        for k in (1, 2, 3):
            filters.append(f"bandreject=f={hum_hz * k}:w={2 * k}")
    if needs_declip:
        filters.append("adeclick")
        filters.append("adeclip")
    filters.append(f"afftdn=nr={nr_db}:nf={nf:.1f}:tn=1")
    i = float(params["deess_intensity"])
    filters.append(f"deesser=i={i}:m=0.5:f=0.5")
    filters.append(
        f"agate=threshold={gate_thr:.1f}dB:range={float(params['gate_range_db'])}dB"
        ":attack=10:release=250"
    )
    return ",".join(filters)


def _process(ctx: StageContext, role: str, params: dict) -> dict:
    # 新規プロジェクトは Trim 後、旧 project.json は Sync 直後を読む。
    input_stage = "trim" if "trim" in ctx.doc["stage_order"] else "sync"
    src = ctx.upstream_dir(input_stage) / f"{role}.flac"
    out = ctx.out_dir / f"{role}.flac"

    ctx.progress(f"cleanup 解析: {role}")
    x = ffmpeg.decode_f32(src, rate=8000)
    noise_floor = analysis.find_noise_floor(x, 8000)
    hum = analysis.detect_hum(x, 8000)
    needs_declip = (
        ctx.doc["stages"]["ingest"]["report"]
        .get("tracks", {}).get(role, {}).get("needs_declip", False)
    )
    chain = build_chain(params, noise_floor, hum, needs_declip)

    # パスB: チェーン適用 + 同時に ebur128 測定(asplit で null 側に分岐)
    ctx.progress(f"cleanup 適用: {role}")
    tmp = ctx.out_dir / f"_{role}_pregain.flac"
    stderr = ffmpeg.run([
        "-i", str(src),
        "-filter_complex",
        f"[0:a]{chain},asplit[keep][meter];[meter]ebur128=peak=none[m]",
        "-map", "[keep]", *ffmpeg.FLAC24, str(tmp),
        "-map", "[m]", "-f", "null", "-",
    ])
    summary = stderr[stderr.rfind("Summary:"):]
    m = ffmpeg._EBUR_I.search(summary)
    if not m:
        raise ffmpeg.FFmpegError(["ebur128", role], stderr)
    measured_i = float(m.group(1))

    # パスC: 純線形ゲインで target LUFS へ(loudnorm は使わない — R-5)
    gain_db = float(params["target_lufs"]) - measured_i
    ctx.progress(f"cleanup ゲイン整合: {role} ({gain_db:+.1f}dB)")
    ffmpeg.run(["-i", str(tmp), "-af", f"volume={gain_db:.2f}dB",
                *ffmpeg.FLAC24, str(out)])
    tmp.unlink()

    ctx.make_preview(out, role, bitrate=PREVIEW_BITRATE_CLEANUP)
    ctx.make_peaks(out, role)
    return {
        "noise_floor_db": round(noise_floor, 1),
        "hum_hz": hum,
        "declip_applied": needs_declip,
        "nr_db": min(float(params["nr_max_db"]), 12.0),
        "loudness_before_gain": measured_i,
        "align_gain_db": round(gain_db, 2),
        "filter_chain": chain,
    }


def run(ctx: StageContext, params: dict) -> dict:
    with ThreadPoolExecutor(max_workers=2) as pool:   # N-4: トラック並列度 2
        results = dict(zip(
            SPEAKERS,
            pool.map(lambda r: _process(ctx, r, params), SPEAKERS),
        ))
    warnings = []
    for role, rep in results.items():
        if rep["hum_hz"]:
            warnings.append(f"{role}: {rep['hum_hz']}Hz ハムを検出しノッチ適用")
        if abs(rep["align_gain_db"]) > 12:
            warnings.append(
                f"{role}: ラウドネス整合ゲインが {rep['align_gain_db']:+.1f}dB と大きい。素材レベルを確認"
            )
    return {"tracks": results, "warnings": warnings}
