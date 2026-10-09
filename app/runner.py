"""ステージ実行ランナー。

- 同時実行ステージ数 1(N-4): モジュール単位の threading.Lock を try-acquire
- ワーカースレッドで実行(ffmpeg サブプロセスと numpy が主役なので GIL は問題にならない)
- GC 済み上流成果物は実行前に自動再生成(決定論 N-5 が前提 — §6.2)
- ffmpeg 異常終了時は stderr 末尾を log_tail に保存し failed へ(§11)
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

from . import project as prj
from . import storage
from .pipeline import ffmpeg
from .pipeline.base import SPEAKERS, StageContext
from .pipeline import (
    s0_ingest, s1_sync, s2_trim, s2_cleanup, s3_dynamics, s4_mix, s5_master, s6_export,
)

STAGE_MODULES = {
    "ingest": s0_ingest, "sync": s1_sync, "trim": s2_trim, "cleanup": s2_cleanup,
    "dynamics": s3_dynamics, "mix": s4_mix, "master": s5_master,
    "export": s6_export,
}

# 各ステージが読む上流と、再生成判定に使う必須成果物
REQUIRED_ARTIFACTS = {
    "ingest": [f"{r}.flac" for r in prj.ROLES],
    "sync": [f"{r}.flac" for r in (*SPEAKERS, "reference")],
    "trim": [f"{r}.flac" for r in (*SPEAKERS, "reference")],
    "cleanup": [f"{r}.flac" for r in SPEAKERS],
    "dynamics": [f"{r}.flac" for r in SPEAKERS],
    "mix": ["mix.flac"],
    "master": ["master.flac"],
    "export": ["episode.wav", "episode.mp3", "episode.m4a"],
}

_busy = threading.Lock()


class Busy(Exception):
    pass


class NotRunnable(Exception):
    pass


def start(project_id: str, stage: str, params: dict | None) -> None:
    """検証してからワーカースレッドを起動する。呼び出し元(API)は即座に返る。"""
    if not _busy.acquire(blocking=False):
        raise Busy("別のステージが実行中です(同時実行は1ステージ)")
    try:
        with prj.update(project_id) as doc:
            if not prj.assets_ready(doc):
                raise NotRunnable("素材6本のアップロードが完了していません")
            statuses = prj.effective_status(doc)
            order = doc["stage_order"]
            if stage not in order:
                raise NotRunnable(f"プロジェクトにステージ {stage} はありません")
            for upstream in order[: order.index(stage)]:
                if statuses[upstream] != "approved":
                    raise NotRunnable(
                        f"上流ステージ {upstream} が未承認です (現在: {statuses[upstream]})"
                    )
            _check_disk(doc)
            st = doc["stages"][stage]
            if params:
                if stage == "trim":
                    # 部分指定でも既定値を失わないように Trim だけはマージする。
                    candidate = dict(st["params"])
                    candidate.update(params)
                    try:
                        s2_trim.resolve_bounds(
                            candidate,
                            doc["stages"]["sync"]["report"].get("program_length_samples", 0),
                        )
                    except ValueError as exc:
                        raise NotRunnable(str(exc)) from exc
                    st["params"] = candidate
                else:
                    st["params"] = params
            elif stage == "trim":
                try:
                    s2_trim.resolve_bounds(
                        st["params"],
                        doc["stages"]["sync"]["report"].get("program_length_samples", 0),
                    )
                except ValueError as exc:
                    raise NotRunnable(str(exc)) from exc
            st["status"] = "running"
            st["progress"] = "開始待ち"
            st["started_at"] = prj.now_iso()
            st["finished_at"] = None
            st["log_tail"] = []
        threading.Thread(
            target=_worker, args=(project_id, stage), daemon=True
        ).start()
    except BaseException:
        _busy.release()
        raise


def _check_disk(doc: dict) -> None:
    """実行前に必要容量を見積もりチェック(§11)。不足時は GC を促す。"""
    asset_bytes = sum(a.get("bytes", 0) for a in doc["assets"].values())
    need = max(1 << 30, int(asset_bytes * 1.5))
    free = storage.disk_free_bytes()
    if free < need:
        raise NotRunnable(
            f"ディスク空きが不足しています(空き {free >> 20}MB / 必要目安 {need >> 20}MB)。"
            "GC(中間成果物の purge)を実行してください"
        )


def _set_progress(project_id: str, stage: str, msg: str) -> None:
    with prj.update(project_id) as doc:
        doc["stages"][stage]["progress"] = msg


def _artifacts_missing(project_id: str, stage: str) -> bool:
    sdir = storage.stage_dir(project_id, stage)
    return any(not (sdir / f).exists() for f in REQUIRED_ARTIFACTS[stage])


def _worker(project_id: str, stage: str) -> None:
    try:
        doc = prj.load(project_id)
        order = doc["stage_order"]

        # GC 済み(または欠損)の上流成果物を上流から順に再生成
        for upstream in order[: order.index(stage)]:
            if _artifacts_missing(project_id, upstream):
                _set_progress(project_id, stage, f"上流 {upstream} を再生成中")
                _execute(project_id, upstream, regenerate=True)

        _execute(project_id, stage, regenerate=False)
    except Exception as exc:
        tail = []
        if isinstance(exc, ffmpeg.FFmpegError):
            tail = exc.stderr.splitlines()[-15:]
        with prj.update(project_id) as doc:
            st = doc["stages"][stage]
            st["status"] = "failed"
            st["finished_at"] = prj.now_iso()
            st["progress"] = ""
            st["log_tail"] = [*tail, f"{type(exc).__name__}: {exc}"]
        traceback.print_exc()
    finally:
        _busy.release()


def _execute(project_id: str, stage: str, regenerate: bool) -> None:
    doc = prj.load(project_id)
    params = doc["stages"][stage]["params"]
    ctx = StageContext(
        project_id=project_id,
        stage=stage,
        doc=doc,
        progress=lambda msg: _set_progress(project_id, stage, msg),
    )
    report = STAGE_MODULES[stage].run(ctx, params)
    ctx.write_report(report)
    fps = prj.expected_fingerprints(doc)
    with prj.update(project_id) as doc2:
        st = doc2["stages"][stage]
        st["report"] = report
        st["artifacts_evicted"] = False
        st["progress"] = ""
        if not regenerate:
            st["status"] = "done"
            st["input_fingerprint"] = fps[stage]
            st["finished_at"] = prj.now_iso()


def approve(project_id: str, stage: str) -> None:
    with prj.update(project_id) as doc:
        status = prj.effective_status(doc)[stage]
        if status != "done":
            raise NotRunnable(f"承認できるのは done のステージのみです (現在: {status})")
        doc["stages"][stage]["status"] = "approved"
    # エピソード完了(export 承認)で直近2ステージ以外を自動 purge(§6.2)
    if stage == "export":
        storage.gc(project_id, keep_recent=2)
