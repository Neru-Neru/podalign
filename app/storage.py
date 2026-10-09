"""パス解決・容量集計・GC(設計 §6)。

永続: 原素材(assets) / preview.opus / peaks.json / report.json / export 成果物
GC 可能: 各ステージの中間 FLAC(決定論的なので上流から再生成できる)
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import project as prj

PERSISTENT_SUFFIXES = {".opus", ".json"}


def stage_dir(project_id: str, stage: str) -> Path:
    # 新規プロジェクトは trim を含む順序、旧 project.json は旧順序のまま。
    # グローバルな STAGE_DIRS だけで解決すると旧 cleanup 以降の成果物を
    # 02_cleanup から 03_cleanup に移してしまうため、保存済み順序を優先する。
    try:
        doc = prj.load(project_id)
        order = doc.get("stage_order", prj.STAGE_ORDER)
        if stage in order:
            return prj.project_dir(project_id) / "stages" / f"{order.index(stage):02d}_{stage}"
    except FileNotFoundError:
        pass
    return prj.project_dir(project_id) / "stages" / prj.STAGE_DIRS[stage]


def asset_path(project_id: str, role: str, doc: dict) -> Path:
    return prj.project_dir(project_id) / doc["assets"][role]["path"]


def usage(project_id: str) -> dict:
    """ディレクトリ別の容量集計(UI のプロジェクト容量表示用)。"""
    pdir = prj.project_dir(project_id)
    out: dict[str, int] = {}
    total = 0
    for sub in ("assets", "stages"):
        base = pdir / sub
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.is_file():
                size = f.stat().st_size
                total += size
                if sub == "assets":
                    key = "assets"
                else:
                    key = f.relative_to(pdir / "stages").parts[0]
                out[key] = out.get(key, 0) + size
    return {"total_bytes": total, "by_dir": out}


def evictable_flacs(project_id: str, stage: str, keep_export: bool = True) -> list[Path]:
    """GC 対象 = ステージ中間 FLAC。preview/peaks/report と export 成果物は残す。"""
    sdir = stage_dir(project_id, stage)
    if not sdir.exists():
        return []
    if stage == "export" and keep_export:
        return []
    return [f for f in sdir.iterdir() if f.suffix == ".flac"]


def gc(project_id: str, keep_recent: int = 2) -> dict:
    """中間 FLAC を purge する。直近 keep_recent ステージ分は残す(再実行が速い)。

    keep_recent=0 で全ステージ purge(手動 purge ボタン)。
    """
    with prj.update(project_id) as doc:
        done_stages = [
            s
            for s in doc["stage_order"]
            if doc["stages"][s]["status"] in ("done", "approved")
            and not doc["stages"][s]["artifacts_evicted"]
        ]
        targets = done_stages[:-keep_recent] if keep_recent else done_stages
        freed = 0
        for stage in targets:
            for f in evictable_flacs(project_id, stage):
                freed += f.stat().st_size
                f.unlink()
            doc["stages"][stage]["artifacts_evicted"] = True
    return {"freed_bytes": freed, "evicted_stages": targets}


def delete_project(project_id: str) -> None:
    shutil.rmtree(prj.project_dir(project_id))


def disk_free_bytes() -> int:
    return shutil.disk_usage(prj.DATA_DIR).free
