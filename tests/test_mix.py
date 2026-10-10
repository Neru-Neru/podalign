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


@pytest.mark.parametrize("tail", [0.0, 3.0, 10.0])
def test_bgm_tail_follows_voice_end_without_changing_start(tail):
    timing = resolve_timing({"bgm_tail_s": tail}, 5.0, 60.0)
    assert timing["bgm_start_s"] == 15.0
    assert timing["bgm_end_s"] == 65.0 + tail
    assert timing["program_length_s"] == 65.0 + tail


def test_unspecified_tail_keeps_existing_timing():
    params = {"bgm_start_s": 1.0}
    assert resolve_timing(params, 5.0, 60.0) == resolve_timing({**params, "bgm_tail_s": None}, 5.0, 60.0)
    assert resolve_timing(params, 5.0, 60.0)["bgm_end_s"] == 61.0


def test_new_projects_default_to_three_seconds(data_dir):
    from app import project as prj

    assert prj.create_project("mix tail")["stages"]["mix"]["params"]["bgm_tail_s"] == 3.0


@pytest.fixture
def mix_context(data_dir):
    from app import project as prj, storage
    from app.pipeline import ffmpeg
    from app.pipeline.base import StageContext

    doc = prj.create_project("short mix")
    doc["assets"] = {role: {} for role in ("speaker_a", "speaker_b")}
    for stage, name, frequency, duration in [
        ("dynamics", "speaker_a", 440, 4), ("dynamics", "speaker_b", 660, 4),
        ("ingest", "jingle", 880, 0.2), ("ingest", "bgm", 220, 2),
    ]:
        directory = storage.stage_dir(doc["id"], stage)
        directory.mkdir(parents=True, exist_ok=True)
        ffmpeg.run(["-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={duration}",
                    "-ar", "48000", *ffmpeg.FLAC24, str(directory / f"{name}.flac")])
    return StageContext(project_id=doc["id"], stage="mix", doc=doc)


@pytest.mark.parametrize("tail", [0.0, 3.0, 10.0])
def test_mix_output_ends_at_configured_tail(mix_context, tail):
    import subprocess

    import numpy as np

    from app.pipeline import ffmpeg, s4_mix

    params = {**mix_context.doc["stages"]["mix"]["params"], "bgm_start_s": 2.2, "bgm_tail_s": tail}
    report = s4_mix.run(mix_context, params)
    output = mix_context.out_dir / "mix.flac"
    end = report["voice_end_s"] + tail
    assert report["bgm_start_s"] == 2.2
    assert report["bgm_end_s"] == pytest.approx(end, abs=1 / 48000)
    assert ffmpeg.probe(output)["duration"] == pytest.approx(end, abs=2 / 48000)
    if tail >= 3:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(output), "-ac", "1", "-f", "f32le", "-"],
            capture_output=True, check=True,
        )
        audio = np.frombuffer(result.stdout, dtype=np.float32)
        def rms(start, stop):
            return np.sqrt(np.mean(audio[int(start * 48000):int(stop * 48000)] ** 2))
        assert rms(end - 0.3, end - 0.1) < rms(end - 2.5, end - 2.3) * 0.3


def test_bgm_start_after_ending_is_rejected(mix_context):
    from app.pipeline import s4_mix

    params = {**mix_context.doc["stages"]["mix"]["params"], "bgm_start_s": 10.0}
    with pytest.raises(ValueError, match="BGM開始"):
        s4_mix.run(mix_context, params)


@pytest.mark.parametrize("key,value", [
    ("jingle_start_s", -0.1), ("voice_start_s", float("nan")),
    ("bgm_start_s", float("inf")), ("bgm_fade_in_s", "2"),
    ("bgm_tail_s", -1), ("bgm_tail_s", float("nan")),
    ("bgm_tail_s", float("inf")), ("bgm_tail_s", "3"), ("bgm_tail_s", True),
])
def test_resolve_timing_rejects_invalid_seconds(key: str, value: object) -> None:
    params = {"jingle_start_s": 0.0, "voice_start_s": None, "bgm_start_s": None, "bgm_fade_in_s": 2.0}
    params[key] = value
    with pytest.raises(ValueError, match="有限の 0 以上"):
        resolve_timing(params, 5.0, 60.0)
