"""再実行の片付けは対象工程に限定し、失敗時の途中ファイルも削除する。"""
from types import SimpleNamespace

import pytest

from app import project as prj, runner, storage


@pytest.mark.parametrize("regenerate", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_execute_cleans_only_target_stage(data_dir, monkeypatch, regenerate, fail):
    doc = prj.create_project("rerun")
    pid = doc["id"]
    stage_dir = storage.stage_dir(pid, "cleanup")
    stage_dir.mkdir(parents=True)
    (stage_dir / "speaker_c.flac").write_bytes(b"obsolete speaker")
    (stage_dir / "preview_speaker_c.opus").write_bytes(b"obsolete preview")
    (stage_dir / "report.json").write_text("old report")
    (stage_dir / "scratch").mkdir()
    (stage_dir / "scratch" / "partial.flac").write_bytes(b"old partial")

    preserved = []
    for stage in ("trim", "dynamics", "export"):
        directory = storage.stage_dir(pid, stage)
        directory.mkdir(parents=True)
        path = directory / ("episode.wav" if stage == "export" else "speaker_a.flac")
        path.write_bytes(b"keep")
        preserved.append(path)
    original = prj.project_dir(pid) / "assets" / "speaker_a.flac"
    original.write_bytes(b"keep")
    preserved.append(original)

    def run(ctx, params):
        assert not stage_dir.exists()
        (ctx.out_dir / "speaker_a.flac").write_bytes(b"new")
        if fail:
            (ctx.out_dir / "_partial.flac").write_bytes(b"partial")
            raise RuntimeError("processing failed")
        return {"tracks": {"speaker_a": {}}}

    monkeypatch.setitem(runner.STAGE_MODULES, "cleanup", SimpleNamespace(run=run))
    if fail:
        with pytest.raises(RuntimeError, match="processing failed"):
            runner._execute(pid, "cleanup", regenerate=regenerate)
        assert not stage_dir.exists()
    else:
        runner._execute(pid, "cleanup", regenerate=regenerate)
        assert {path.name for path in stage_dir.iterdir()} == {"speaker_a.flac", "report.json"}
        assert prj.load(pid)["stages"]["cleanup"]["report"] == {"tracks": {"speaker_a": {}}}
    assert all(path.read_bytes() == b"keep" for path in preserved)


def test_export_keeps_completed_files_on_failure(data_dir, monkeypatch):
    pid = prj.create_project("export")["id"]
    directory = storage.stage_dir(pid, "export")
    directory.mkdir(parents=True)
    completed = directory / "episode.wav"
    completed.write_bytes(b"completed")

    def run(ctx, params):
        assert completed.read_bytes() == b"completed"
        raise RuntimeError("export failed")

    monkeypatch.setitem(runner.STAGE_MODULES, "export", SimpleNamespace(run=run))
    with pytest.raises(RuntimeError, match="export failed"):
        runner._execute(pid, "export", regenerate=False)
    assert completed.read_bytes() == b"completed"
