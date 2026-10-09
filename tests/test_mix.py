from __future__ import annotations

import pytest

from app.pipeline.s4_mix import resolve_timing


def test_resolve_timing_default_tracks_jingle_and_voice() -> None:
    timing = resolve_timing(
        {"jingle_start_s": 0.0, "voice_start_s": None, "bgm_start_s": None, "bgm_fade_in_s": 2.0},
        jingle_duration_s=5.0,
        voice_duration_s=60.0,
    )
    assert timing == {
        "jingle_start_s": 0.0, "voice_start_s": 5.0, "bgm_start_s": 15.0,
        "bgm_fade_in_s": 2.0, "jingle_end_s": 5.0, "voice_end_s": 65.0,
        "bgm_end_s": 75.0, "program_length_s": 75.0,
    }


def test_resolve_timing_manual_values_are_fixed_when_jingle_changes() -> None:
    params = {"jingle_start_s": 3.0, "voice_start_s": 20.0, "bgm_start_s": 40.0, "bgm_fade_in_s": 1.5}
    timing = resolve_timing(params, jingle_duration_s=12.0, voice_duration_s=30.0)
    assert timing["jingle_end_s"] == 15.0
    assert timing["voice_start_s"] == 20.0
    assert timing["bgm_start_s"] == 40.0
    assert timing["program_length_s"] == 70.0


def test_resolve_timing_auto_voice_recalculates_when_jingle_changes() -> None:
    params = {"jingle_start_s": 1.0, "voice_start_s": None, "bgm_start_s": None, "bgm_fade_in_s": 2.0}
    assert resolve_timing(params, 4.0, 20.0)["voice_start_s"] == 5.0
    changed = resolve_timing(params, 9.0, 20.0)
    assert changed["voice_start_s"] == 10.0
    assert changed["bgm_start_s"] == 20.0


@pytest.mark.parametrize("key,value", [
    ("jingle_start_s", -0.1), ("voice_start_s", float("nan")),
    ("bgm_start_s", float("inf")), ("bgm_fade_in_s", "2"),
])
def test_resolve_timing_rejects_invalid_seconds(key: str, value: object) -> None:
    params = {"jingle_start_s": 0.0, "voice_start_s": None, "bgm_start_s": None, "bgm_fade_in_s": 2.0}
    params[key] = value
    with pytest.raises(ValueError, match="有限の 0 以上"):
        resolve_timing(params, 5.0, 60.0)
