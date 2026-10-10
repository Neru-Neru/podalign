"""パイプライン一気通貫テスト(設計 §12.2 / §12.3 / §12.5 の縮約版)。

60秒の合成素材5〜6本を全ステージに通し、
- Sync: 既知オフセットの復元と残差 ±1ms
- Master: Integrated -16 ±0.5 LUFS / True Peak ≤ -1.0 dBTP(ebur128 実測)
- Export: 3フォーマット生成
- GC 後の再実行で上流から正しく再生成されること
を検証する。実行時間は数分(ffmpeg 実処理を含む)。
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from app import project as prj
from app import runner, storage
from app.pipeline import ffmpeg as ff
from tests.test_sync import speechlike

SR = 48000
DUR = 60.0
OFFSETS = {"speaker_a": 0.8, "speaker_b": -0.4, "speaker_c": 1.5}


def _write_wav(path: Path, x: np.ndarray, sr: int = SR) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error",
         "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-",
         "-c:a", "pcm_s16le", str(path)],
        input=np.clip(x, -1, 1).astype(np.float32).tobytes(), check=True,
    )


@pytest.fixture(params=[2, 3], ids=["two-speakers", "three-speakers"])
def project(data_dir, request) -> dict:
    doc = prj.create_project("e2e")
    pid = doc["id"]
    assets = prj.project_dir(pid) / "assets"

    # 2〜3話者: 帯域の違う発話様バースト。reference = 各話者を「話者が先行」する形で合成
    # (off = 話者時刻 − ref 時刻 > 0 → ref 側では話者素材の off 秒目から始まる)
    rng = np.random.default_rng(7)
    roles = prj.SPEAKERS[:request.param]
    voices = {}
    for i, role in enumerate(roles):
        v = speechlike(seed=10 + i, dur=DUR, sr=SR) * 0.5
        voices[role] = v
    n = int(DUR * SR)
    ref = rng.standard_normal(n).astype(np.float32) * 1e-4
    for role in roles:
        off = OFFSETS[role]
        shift = int(round(off * SR))
        v = voices[role]
        if shift >= 0:
            seg = v[shift:]
            ref[: len(seg)] += seg
        else:
            ref[-shift : -shift + len(v)][: n + shift] += v[: n + shift]
    for role in roles:
        _write_wav(assets / f"{role}.src.wav", voices[role])
    _write_wav(assets / "reference.src.wav", ref)

    t = np.arange(int(5 * SR)) / SR
    jingle = (0.3 * np.sin(2 * np.pi * 440 * t) * np.hanning(len(t))).astype(np.float32)
    _write_wav(assets / "jingle.src.wav", jingle)
    t = np.arange(int(8 * SR)) / SR
    bgm = (0.2 * (np.sin(2 * np.pi * 220 * t) + 0.5 * np.sin(2 * np.pi * 330 * t))).astype(np.float32)
    _write_wav(assets / "bgm.src.wav", bgm)

    with prj.update(pid) as doc:
        for role in [*roles, "reference", "jingle", "bgm"]:
            src = assets / f"{role}.src.wav"
            ff.run(["-i", str(src), "-c:a", "flac", str(assets / f"{role}.flac")])
            doc["assets"][role] = {
                "path": f"assets/{role}.flac",
                "bytes": src.stat().st_size,
                "received": src.stat().st_size,
                "status": "ready",
                "sha256": prj.sha256_file(src),
            }
            src.unlink()
    return prj.load(pid)


def _run_and_approve(pid: str, stage: str) -> dict:
    runner.start(pid, stage, None)
    for _ in range(600):
        time.sleep(1)
        status = prj.load(pid)["stages"][stage]["status"]
        if status in ("done", "failed"):
            break
    doc = prj.load(pid)
    st = doc["stages"][stage]
    assert st["status"] == "done", f"{stage}: {st['log_tail']}"
    runner.approve(pid, stage)
    return st["report"]


def test_full_pipeline(project):
    pid = project["id"]

    _run_and_approve(pid, "ingest")

    sync_report = _run_and_approve(pid, "sync")
    assert sync_report["program_length_samples"] > 0
    sync_dir = storage.stage_dir(pid, "sync")
    # Trim編集画面が初回から読む話者とreferenceの共通時間軸成果物。
    reference_info = ff.probe(sync_dir / "reference.flac")
    assert reference_info["duration"] == pytest.approx(
        sync_report["program_length_samples"] / SR, abs=1 / SR
    )
    assert (sync_dir / "preview_reference.opus").exists()
    assert (sync_dir / "peaks_reference.json").exists()
    assert set(sync_report["offsets_ms"]) == set(prj.speaker_roles(project))
    for role in prj.speaker_roles(project):
        expected = OFFSETS[role]
        got = sync_report["offsets_ms"][role] / 1e3
        assert abs(got - expected) < 1.5e-3, f"{role}: {got} vs {expected}"
        assert abs(sync_report["residual_ms"][role]) <= 1.0
        # 60秒素材にドリフトは仕込んでいない(ドリフトは test_sync で検証済み)
        assert not sync_report["drift_corrected"][role]

    trim_report = _run_and_approve(pid, "trim")
    assert trim_report["applied_start_s"] == 0.0
    assert trim_report["end_sample"] == sync_report["program_length_samples"]
    assert trim_report["output_length_s"] == pytest.approx(trim_report["program_length_s"], abs=1 / SR)
    trim_dir = storage.stage_dir(pid, "trim")
    trim_lengths = []
    for role in (*prj.speaker_roles(project), "reference"):
        assert (trim_dir / f"{role}.flac").exists()
        info = ff.probe(trim_dir / f"{role}.flac")
        trim_lengths.append(info["duration"])
        assert info["start_time"] == pytest.approx(0.0, abs=1e-6)
    assert max(trim_lengths) - min(trim_lengths) <= 2 / SR

    _run_and_approve(pid, "cleanup")
    _run_and_approve(pid, "dynamics")
    mix_report = _run_and_approve(pid, "mix")
    # 既定の絶対タイムライン: ジングルのみ → 話者 → 10秒後にBGM(2秒フェード)。
    assert mix_report["jingle_start_s"] == 0.0
    assert mix_report["voice_start_s"] == pytest.approx(5.0, abs=1 / SR)
    assert mix_report["bgm_start_s"] == pytest.approx(15.0, abs=1 / SR)
    assert mix_report["bgm_fade_in_s"] == 2.0
    assert mix_report["bgm_end_s"] == pytest.approx(mix_report["voice_end_s"] + 3.0, abs=0.001)
    mix_info = ff.probe(storage.stage_dir(pid, "mix") / "mix.flac")
    # レポートの秒数は小数3桁へ丸めている。
    assert mix_info["duration"] == pytest.approx(mix_report["program_length_s"], abs=0.0005 + 2 / SR)

    master_report = _run_and_approve(pid, "master")
    final = master_report["final"]
    assert abs(final["I"] - (-16.0)) <= 0.5, f"Integrated {final['I']} LUFS"
    assert final["TP"] <= -1.0, f"True Peak {final['TP']} dBTP"

    export_report = _run_and_approve(pid, "export")
    exp_dir = storage.stage_dir(pid, "export")
    for fmt in ("wav", "mp3", "aac"):
        assert (exp_dir / export_report["artifacts"][fmt]["file"]).exists()

    # --- GC → 再実行で上流から再生成される(§12.3) ---
    # export 承認時に直近2ステージ以外は自動 purge されている(§6.2)
    doc = prj.load(pid)
    evicted = [s for s in doc["stage_order"] if doc["stages"][s]["artifacts_evicted"]]
    assert "ingest" in evicted
    storage.gc(pid, keep_recent=0)  # 手動 purge で全ステージ分を破棄
    assert not (storage.stage_dir(pid, "master") / "master.flac").exists()

    runner.start(pid, "export", None)
    for _ in range(600):
        time.sleep(1)
        if prj.load(pid)["stages"]["export"]["status"] in ("done", "failed"):
            break
    doc = prj.load(pid)
    assert doc["stages"]["export"]["status"] == "done", doc["stages"]["export"]["log_tail"]
    # 再生成された master が同じラウドネスを持つ(決定論 N-5)
    regen = ff.measure_ebur128(storage.stage_dir(pid, "master") / "master.flac")
    assert abs(regen["I"] - final["I"]) < 0.2
