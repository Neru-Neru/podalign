"""Deterministic small-anchor regression, independent of edit-radio source."""
from __future__ import annotations

import numpy as np


def fit_clock(t: np.ndarray, lag: np.ndarray, weights: np.ndarray, keep: np.ndarray, method: str):
    if method not in ("theil_sen", "ransac"):
        raise ValueError("同期推定方式が不正です")
    idx = np.flatnonzero(keep)
    if len(idx) < 3 or np.ptp(t[idx]) <= 0 or not np.all(np.isfinite(lag)):
        raise ValueError("同期推定に十分な独立アンカーがありません")
    pairs = [(i, j) for k, i in enumerate(idx) for j in idx[k+1:] if t[j] - t[i] >= 1]
    if not pairs:
        raise ValueError("同期アンカーの時間間隔が不足しています")
    slopes = np.array([(lag[j] - lag[i]) / (t[j] - t[i]) for i, j in pairs])
    slope = float(np.median(slopes))
    intercept = float(np.median(lag[idx] - slope * t[idx]))
    if method == "ransac":
        best = None
        for (i, j), candidate in zip(pairs, slopes):
            if abs(candidate) > 500e-6:
                continue
            origin = lag[i] - candidate * t[i]
            mask = keep & (np.abs(lag - (candidate * t + origin)) <= 0.001)
            count = int(mask.sum())
            if count < 3:
                continue
            # Count and temporal span first: one huge confidence cannot dominate.
            score = (count, float(np.ptp(t[mask])), float(np.minimum(weights[mask], 100).sum()))
            if best is None or score > best[0]:
                best = (score, mask, float(candidate), float(origin))
        if best is None:
            raise ValueError("RANSACで独立した合意が得られませんでした")
        _, keep, slope, intercept = best
    for _ in range(3):
        resid = lag - (slope * t + intercept)
        mad = float(np.median(np.abs(resid[keep]))) + 1e-9
        new = keep & (np.abs(resid) < max(5 * 1.4826 * mad, 0.5e-3))
        if new.sum() >= 3:
            keep = new
        slope, intercept = map(float, np.polyfit(t[keep], lag[keep], 1, w=np.sqrt(weights[keep])))
    return slope, intercept, keep
