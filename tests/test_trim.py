"""Trim の境界検証とサンプル単位のフィルタ仕様。"""
from __future__ import annotations

import pytest

from app.pipeline.s2_trim import resolve_bounds, trim_filter


def test_trim_defaults_to_sync_program_length():
    samples = 592_560
    bounds = resolve_bounds({"start_s": 0.0, "end_s": None}, samples)
    assert bounds["start_sample"] == 0
    assert bounds["end_sample"] == samples
    assert bounds["program_length_samples"] == samples
    assert bounds["output_length_s"] == pytest.approx(12.345, abs=1 / 48000)


def test_trim_rounds_boundaries_to_48khz_samples():
    bounds = resolve_bounds({"start_s": 1.000001, "end_s": 2.000001}, 480_000)
    assert bounds["start_sample"] == 48000
    assert bounds["end_sample"] == 96000
    assert bounds["start_s"] == 1.0
    assert bounds["end_s"] == 2.0
    assert trim_filter(bounds["start_sample"], bounds["end_sample"]) == (
        "atrim=start_sample=48000:end_sample=96000,asetpts=PTS-STARTPTS"
    )


def test_trim_rounds_half_samples_like_javascript_math_round():
    bounds = resolve_bounds({"start_s": 0.5 / 48000, "end_s": 10.5 / 48000}, 480_000)
    assert bounds["start_sample"] == 1
    assert bounds["end_sample"] == 11


@pytest.mark.parametrize(
    "params",
    [
        {"start_s": 3.0, "end_s": 3.0},
        {"start_s": 4.0, "end_s": 3.0},
        {"start_s": -0.1, "end_s": 2.0},
        {"start_s": 0.0, "end_s": 10.1},
    ],
)
def test_trim_rejects_invalid_bounds(params):
    with pytest.raises(ValueError):
        resolve_bounds(params, 480_000)
