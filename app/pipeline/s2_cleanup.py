"""Stage 3 — Cleanup(話者ごとの整音)。順序が音質を決める(§7)。

1. ハイパス 80Hz(DC も落ちる) 2. ハム自動検出→ノッチ 3. declick/declip(QC 検出時)
4. afftdn(ノイズフロア自動推定、削減量 12dB クランプ) 5. ディエッサー
6. ノイズゲート(range で -12dB に留める)
7. 話者間ラウドネス整合: ebur128 測定 + volume の純線形ゲインで -20 LUFS(R-5)
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import analysis, ffmpeg
from .base import PREVIEW_BITRATE_CLEANUP, StageContext
from .gain import GainPolicy, choose_gain, finite_setting
from .denoise import verified_nr
from .voice import bleed_pairs


def build_chain(
    params: dict, noise_floor_db: float, hum_hz: int | None, needs_declip: bool
) -> str:
    nr_db = min(float(params["nr_max_db"]), 12.0)   # かけすぎ=水中音が最大の失敗モード
    nf = max(-80.0, min(-20.0, noise_floor_db))
    gate_thr = max(-90.0, nf + 6.0)
    hp = finite_setting(params["highpass_hz"], "highpass_hz", 20, 200)
    filters = [f"highpass=f={hp:.3f}:p=2"]
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
    policy = GainPolicy.from_params(params, role)
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

    # Keep the pre-gain signal in floating point: FLAC would clip peaks before
    # we can measure them and choose a safe linear gain.
    ctx.progress(f"cleanup 適用: {role}")
    tmp = ctx.out_dir / f"_{role}_pregain.wav"
    nr_report: dict = {"requested_mode": "legacy", "nr_db": min(float(params["nr_max_db"]), 12), "warnings": []}
    overrides = params.get("track_denoise_mode", {})
    if not isinstance(overrides, dict) or any(k not in ("speaker_a", "speaker_b", "speaker_c") for k in overrides):
        raise ValueError("話者別ノイズ除去設定が不正です")
    mode = overrides.get(role, params.get("denoise_mode", "legacy"))
    filters = chain.split(",")
    idx = next((i for i, f in enumerate(filters) if f.startswith("afftdn=")), len(filters))
    if idx == len(filters):
        # A chain with no NR has no NR delay to compensate.
        ffmpeg.run(["-i", str(src), "-af", chain, "-c:a", "pcm_f32le", "-rf64", "auto", str(tmp)])
    else:
        pre_chain = ",".join(filters[:idx]) or "anull"
        post_chain = ",".join(filters[idx+1:]) or "anull"
        pre, nr = ctx.out_dir / f"_{role}_pre.wav", ctx.out_dir / f"_{role}_nr.wav"
        ffmpeg.run(["-i", str(src), "-af", pre_chain, "-c:a", "pcm_f32le", "-rf64", "auto", str(pre)])
        protection = params.get("speech_band_protection", True)
        if not isinstance(protection, bool):
            raise ValueError("音声帯域保護は真偽値で指定してください")
        maximum = finite_setting(params["nr_max_db"], "nr_max_db", 0, 12)
        # Preserve legacy strength/profile selection, while making timing and
        # post-NR degradation checks common to every mode.
        nr_report = verified_nr(pre, nr, mode, maximum, protection if mode != "legacy" else False,
                                noise_db_override=noise_floor if mode == "legacy" else None)
        pre.unlink()
        ffmpeg.run(["-i", str(nr), "-af", post_chain, "-c:a", "pcm_f32le", "-rf64", "auto", str(tmp)])
        nr.unlink()
    measured = ffmpeg.measure_ebur128(tmp)
    decision = choose_gain(measured["I"], measured["TP"], policy)
    gain_db = decision.applied_gain_db
    ctx.progress(f"cleanup ゲイン整合: {role} ({gain_db:+.1f}dB)")
    ffmpeg.run(["-i", str(tmp), "-af", f"volume={gain_db:.6f}dB",
                *ffmpeg.FLAC24, str(out)])
    tmp.unlink()
    final = ffmpeg.measure_ebur128(out)
    if final["TP"] is None and measured["TP"] is not None:
        raise ValueError(f"{role}: Cleanup 出力の True Peak を検証できません")
    if final["TP"] is not None and final["TP"] > policy.peak_ceiling_dbtp + 0.05:
        raise ValueError(f"{role}: Cleanup 出力の True Peak が安全上限を超えました")

    ctx.make_preview(out, role, bitrate=PREVIEW_BITRATE_CLEANUP)
    ctx.make_peaks(out, role)
    return {
        "noise_floor_db": round(noise_floor, 1),
        "hum_hz": hum,
        "declip_applied": needs_declip,
        "nr_db": nr_report["nr_db"],
        "denoise": nr_report,
        "loudness_before_gain": measured["I"],
        "true_peak_before_gain": measured["TP"],
        "loudness_after_gain": final["I"],
        "true_peak_after_gain": final["TP"],
        "requested_gain_db": decision.requested_gain_db,
        "gain_limit_reasons": list(decision.reasons),
        "gain_policy": policy.report(),
        "align_gain_db": round(gain_db, 2),
        "applied_gain_db": float(f"{gain_db:.6f}"),
        "filter_chain": chain,
        "nr_latency_compensated": idx < len(filters),
    }


def run(ctx: StageContext, params: dict) -> dict:
    with ThreadPoolExecutor(max_workers=2) as pool:   # N-4: トラック並列度 2
        results = dict(zip(
            ctx.speakers,
            pool.map(lambda r: _process(ctx, r, params), ctx.speakers),
        ))
    reason_labels = {
        "max_gain": "最大増幅量",
        "peak_headroom": "ピーク上限",
        "below_silence_gate": "無音レベル",
        "loudness_unmeasurable": "音量を測定できません",
    }
    warnings: list[str] = []
    for role, rep in results.items():
        warnings.extend(f"{role}: {w}" for w in rep["denoise"]["warnings"])
        if rep["hum_hz"]:
            warnings.append(f"{role}: {rep['hum_hz']}Hz ハムを検出しノッチ適用")
        if rep["gain_limit_reasons"]:
            reasons = "、".join(reason_labels[reason] for reason in rep["gain_limit_reasons"])
            warnings.append(
                f"{role}: 安全のためゲインを制限 ({reasons})。"
                f"目標 {rep['gain_policy']['target_lufs']} LUFS / "
                f"実測 {rep['loudness_after_gain']} LUFS"
            )
        if abs(rep["align_gain_db"]) > 12:
            warnings.append(
                f"{role}: ラウドネス整合ゲインが {rep['align_gain_db']:+.1f}dB と大きい。素材レベルを確認"
            )
    input_stage = "trim" if "trim" in ctx.doc["stage_order"] else "sync"
    pairs = bleed_pairs({r: ffmpeg.decode_f32(ctx.upstream_dir(input_stage) / f"{r}.flac", rate=8000)
                         for r in ctx.speakers})
    for pair in pairs:
        warnings.append(f"{' / '.join(pair['tracks'])}: マイクかぶり・重複音声・極性の確認が必要です "
                        f"(相関 {pair['correlation']:.2f}、遅延 {pair['lag_ms']:.2f}ms)")
    return {"tracks": results, "bleed_pairs": pairs, "warnings": warnings}
