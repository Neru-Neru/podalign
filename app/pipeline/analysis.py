"""numpy による解析(音の加工はしない)。

- GCC-PHAT によるオフセット推定(設計 §7 Stage 1)
- 区間別オフセット → 外れ値除去付き重み付き最小二乗によるドリフト推定
- ノイズフロア推定 / ハム周波数検出(Stage 2)
- wavesurfer 用 peaks 生成
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import ffmpeg

ANALYSIS_SR = 8000       # 微調整用ダウンサンプル周波数(±0.125ms 分解能 + 放物線補間)
ENV_HOP_S = 0.02         # エンベロープ 20ms ホップ = 50Hz


def envelope_db(x: np.ndarray, sr: int, hop_s: float = ENV_HOP_S) -> np.ndarray:
    """20ms ホップの RMS を dB 化し平均減算した 50Hz エンベロープ。"""
    hop = int(sr * hop_s)
    n = len(x) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    frames = x[: n * hop].reshape(n, hop).astype(np.float64)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-12)
    return db - db.mean()


def _taper(x: np.ndarray, sr: int, ramp_s: float = 0.5) -> np.ndarray:
    """両端に半ハン窓ランプを掛ける。

    ゼロパディング境界の段差は広帯域コヒーレントで、PHAT がこれを増幅して
    「lag = 長さ差」に真のピークより高い偽ピークを立てる(実測で確認)。
    """
    n = len(x)
    ramp = min(int(sr * ramp_s), n // 8)
    if ramp < 2:
        return x
    w = np.ones(n)
    half = np.hanning(2 * ramp)
    w[:ramp] = half[:ramp]
    w[-ramp:] = half[ramp:]
    return x * w


def gcc_phat(
    a: np.ndarray, b: np.ndarray, max_lag: int | None = None
) -> tuple[float, float]:
    """GCC-PHAT: b に対する a の遅れ(サンプル数、放物線補間でサブサンプル)と信頼度。

    信頼度 = ピーク高さ / 相関系列の RMS。無相関だと ~3–5、明確な一致で数十。
    """
    n = len(a) + len(b)
    nfft = 1 << (n - 1).bit_length()
    fa = np.fft.rfft(a, nfft)
    fb = np.fft.rfft(b, nfft)
    g = fa * np.conj(fb)
    g /= np.abs(g) + 1e-12                      # PHAT 重み = 位相のみ残す
    r = np.fft.irfft(g, nfft)
    lags = np.concatenate([np.arange(nfft // 2), np.arange(-nfft // 2, 0)])
    if max_lag is not None:
        mask = np.abs(lags) > max_lag
        r = r.copy()
        r[mask] = 0.0
    idx = int(np.argmax(r))
    peak = r[idx]
    noise = np.sqrt(np.mean(r**2) + 1e-18)
    confidence = float(peak / noise)
    # 放物線補間でサブサンプル精度に
    y0, y1, y2 = r[(idx - 1) % nfft], r[idx], r[(idx + 1) % nfft]
    denom = y0 - 2 * y1 + y2
    frac = 0.0 if abs(denom) < 1e-18 else float(0.5 * (y0 - y2) / denom)
    frac = float(np.clip(frac, -0.5, 0.5))
    return float(lags[idx]) + frac, confidence


def cross_correlate(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """素の相互相関(PHAT なし)による lag と信頼度。

    エンベロープの粗探索はこちらを使う: PHAT は白色化により
    「その話者が写っていない区間」の不一致成分まで等重み化し、
    エンベロープ段では相関がほぼ平坦になって偽ピークに負ける(実測)。
    素の相関はバーストのエネルギーで自然に重み付けされ頑健。
    """
    n = len(a) + len(b)
    nfft = 1 << (n - 1).bit_length()
    fa = np.fft.rfft(a, nfft)
    fb = np.fft.rfft(b, nfft)
    r = np.fft.irfft(fa * np.conj(fb), nfft)
    lags = np.concatenate([np.arange(nfft // 2), np.arange(-nfft // 2, 0)])
    idx = int(np.argmax(r))
    noise = np.sqrt(np.mean(r**2) + 1e-18)
    return float(lags[idx]), float(r[idx] / noise)


@dataclass
class SyncEstimate:
    offset_s: float          # d0: reference t=0 における話者の進み(秒)
    drift_ppm: float         # s×1e6: 「時間 vs ずれ」の傾き
    segments: list[dict] = field(default_factory=list)   # UI グラフ用
    used_segments: int = 0
    warnings: list[str] = field(default_factory=list)


def estimate_sync(
    spk: np.ndarray,
    ref: np.ndarray,
    sr: int = ANALYSIS_SR,
    n_segments: int = 10,
    segment_s: float = 30.0,
    fine_window_s: float = 0.1,
) -> SyncEstimate:
    """話者トラックの reference に対するオフセット+ドリフトを推定する。

    1. エンベロープ(50Hz)同士の GCC-PHAT フルレンジ相関 → 粗オフセット(±20ms)
    2. 収録を n_segments 区間に分け、各区間の 8kHz 波形を粗オフセット周辺で
       GCC-PHAT → サンプル精度の区間別オフセット
    3. 「時間 vs ずれ」を相関ピーク信頼度で重み付けし、外れ値除去付き
       最小二乗でフィット。傾き=ドリフト率、切片=初期オフセット
    """
    warnings: list[str] = []
    hop = int(sr * ENV_HOP_S)

    # --- 1. 粗探索(エンベロープ、素の相互相関 — cross_correlate の docstring 参照) ---
    env_s, env_r = envelope_db(spk, sr), envelope_db(ref, sr)
    coarse_hops, coarse_conf = cross_correlate(env_s, env_r)
    coarse_s = coarse_hops * ENV_HOP_S
    if coarse_conf < 5.0:
        warnings.append(
            f"粗探索の相関が弱い (confidence={coarse_conf:.1f})。素材の対応を確認してください"
        )

    # --- 2. 区間別の微調整 ---
    # off = 話者時刻 − ref 時刻。話者が後から録音開始なら off<0 で ref 冒頭に対応点が無い
    overlap_start = max(0.0, -coarse_s)                    # ref 時間軸での重なり
    overlap_end = min(len(ref) / sr, (len(spk) / sr) - coarse_s)
    usable = overlap_end - overlap_start
    if usable < segment_s * 2:
        raise ValueError(f"重なり区間が短すぎます ({usable:.1f}s)")

    seg_starts = np.linspace(
        overlap_start, overlap_end - segment_s, n_segments
    )
    # 探索窓: 粗オフセット誤差(±20ms)に加え、ドリフト累積(最大150ppm級)で
    # 区間別オフセットが粗推定から最大±数百ms 逸れうる分を確保する
    margin = max(fine_window_s, 250e-6 * overlap_end)
    times, lags, weights, activity = [], [], [], []
    segments = []
    for t0 in seg_starts:
        r_seg = ref[int(t0 * sr) : int((t0 + segment_s) * sr)]
        s_from = (t0 + coarse_s) - margin
        s_seg_start = int(s_from * sr)
        s_seg = spk[max(0, s_seg_start) : int((s_from + segment_s + 2 * margin) * sr)]
        if len(r_seg) < sr or len(s_seg) < sr:
            continue
        lag_samples, conf = gcc_phat(
            _taper(s_seg.astype(np.float64), sr),
            _taper(r_seg.astype(np.float64), sr),
            max_lag=int(2.5 * margin * sr),
        )
        # s_seg は ref 区間より (coarse_s - margin) 早くから切ってあるため補正
        lag_s = lag_samples / sr + (coarse_s - margin) + (max(0, s_seg_start) - s_seg_start) / sr
        t_mid = t0 + segment_s / 2
        rms_db = float(10 * np.log10(np.mean(s_seg.astype(np.float64) ** 2) + 1e-12))
        times.append(t_mid)
        lags.append(lag_s)
        weights.append(conf)
        activity.append(rms_db)
        segments.append({"t": round(t_mid, 3), "lag_ms": round(lag_s * 1e3, 4),
                         "confidence": round(conf, 2), "rms_db": round(rms_db, 1)})

    if len(times) < 3:
        raise ValueError("有効な区間が3未満でドリフトを推定できません")

    t_arr = np.array(times)
    l_arr = np.array(lags)
    w_arr = np.array(weights)
    a_arr = np.array(activity)

    # --- 3. 外れ値除去付きロバストフィット ---
    # その話者が喋っていない区間を落とす:
    #  (a) 話者側 RMS が最大区間より 35dB 以上低い(物理的な無音判定)
    #  (b) PHAT ピーク/RMS 比が乱数水準(FFT 長 2^21 で ~5.5)に近い
    # 相対足切り(0.25×max 等)は使わない — 実発話のピークは数百に達し、
    # 正常区間まで除外してフィットを壊す(test_silence_robustness で実証)
    keep = (a_arr > a_arr.max() - 35.0) & (w_arr >= 8.0)
    if keep.sum() < 3:
        raise ValueError(
            "全区間で相関が立ちません(長時間無音の可能性)。手動オフセットを検討してください"
        )
    if (~keep).any():
        warnings.append(f"{int((~keep).sum())} 区間を低信頼として除外")

    # 初期フィットは Theil–Sen(ペアワイズ傾きの中央値)— 残存外れ値に強い
    idx = np.where(keep)[0]
    pair_slopes = [
        (l_arr[j] - l_arr[i]) / (t_arr[j] - t_arr[i])
        for k, i in enumerate(idx) for j in idx[k + 1 :]
    ]
    slope = float(np.median(pair_slopes))
    intercept = float(np.median(l_arr[keep] - slope * t_arr[keep]))

    for _ in range(3):  # 残差ベースの外れ値除去 → 重み付き最小二乗で磨く
        resid = l_arr - (slope * t_arr + intercept)
        mad = np.median(np.abs(resid[keep])) + 1e-9
        new_keep = keep & (np.abs(resid) < max(5 * 1.4826 * mad, 0.5e-3))
        if new_keep.sum() >= 3:
            keep = new_keep
        coef = np.polyfit(t_arr[keep], l_arr[keep], 1, w=np.sqrt(w_arr[keep]))
        slope, intercept = float(coef[0]), float(coef[1])
    for seg, k in zip(segments, keep):
        seg["used"] = bool(k)
    return SyncEstimate(
        offset_s=intercept,
        drift_ppm=slope * 1e6,
        segments=segments,
        used_segments=int(keep.sum()),
        warnings=warnings,
    )


def find_noise_floor(x: np.ndarray, sr: int, win_s: float = 0.5) -> float:
    """最も静かな窓群の RMS 中央値をノイズフロア(dBFS)として返す(§7 Stage 2-5)。"""
    win = int(sr * win_s)
    n = len(x) // win
    if n < 4:
        return -60.0
    frames = x[: n * win].reshape(n, win).astype(np.float64)
    rms_db = 10 * np.log10(np.mean(frames**2, axis=1) + 1e-12)
    k = max(3, n // 20)                       # 下位 5% の窓を採用
    quiet = np.sort(rms_db)[:k]
    return float(np.clip(np.median(quiet), -90.0, -20.0))


def detect_hum(x: np.ndarray, sr: int) -> int | None:
    """50/60Hz ハムを検出。周囲帯域より 10dB 以上突出していればその周波数を返す。"""
    n = min(len(x), sr * 60)                  # 最初の60秒で判定
    if n < sr * 5:
        return None
    seg = x[:n].astype(np.float64)
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    freq = np.fft.rfftfreq(len(seg), 1 / sr)

    def band_power(f0: float, width: float) -> float:
        m = (freq >= f0 - width) & (freq <= f0 + width)
        return float(spec[m].max()) if m.any() else 0.0

    best = None
    for f0 in (50, 60):
        hum = band_power(f0, 1.0)
        neighbor = max(band_power(f0 - 8, 3.0), band_power(f0 + 8, 3.0), 1e-18)
        ratio_db = 10 * np.log10(hum / neighbor)
        if ratio_db > 10 and (best is None or ratio_db > best[1]):
            best = (f0, ratio_db)
    return best[0] if best else None


def silence_ratio(x: np.ndarray, sr: int, threshold_db: float = -55.0) -> float:
    env = envelope_db(x, sr)
    if len(env) == 0:
        return 1.0
    # envelope_db は平均減算済みなので絶対レベルを再計算
    hop = int(sr * ENV_HOP_S)
    n = len(x) // hop
    frames = x[: n * hop].reshape(n, hop).astype(np.float64)
    db = 10 * np.log10(np.mean(frames**2, axis=1) + 1e-12)
    return float(np.mean(db < threshold_db))


def compute_peaks(path: str | Path, n_points: int = 2400) -> list[float]:
    """波形表示用に区間最大絶対値を n_points 個計算(ストリーミング、RAM 定数)。"""
    info = ffmpeg.probe(path)
    sr = 8000
    total = max(1, int(info["duration"] * sr))
    per_bin = max(1, total // n_points)
    peaks: list[float] = []
    carry = np.zeros(0, dtype=np.float32)
    for chunk in ffmpeg.stream_f32(path, rate=sr, mono=True):
        buf = np.concatenate([carry, np.abs(chunk)])
        n_full = len(buf) // per_bin
        if n_full:
            peaks.extend(
                buf[: n_full * per_bin].reshape(n_full, per_bin).max(axis=1).tolist()
            )
        carry = buf[n_full * per_bin :]
    if len(carry):
        peaks.append(float(carry.max()))
    return [round(p, 4) for p in peaks]
