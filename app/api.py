"""API ルーティング(設計 §8)。

- 進捗は GET /api/projects/{id} の1秒ポーリング(SSE/WebSocket は不採用 — §4.2)
- チャンクアップロードは raw octet-stream の PUT(python-multipart 不要 — R-7)
- 中間 FLAC はブラウザに送らない。配信は preview.opus / peaks.json のみ
- ダウンロードはホワイトリスト照合のみでパス結合をしない(R-11)
"""
from __future__ import annotations

import hashlib
import re
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from . import project as prj
from . import runner, storage
from .pipeline import ffmpeg

router = APIRouter(prefix="/api")

_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def _load_or_404(project_id: str) -> dict:
    if not _NAME_RE.match(project_id.replace("-", "_")):
        raise HTTPException(404)
    try:
        return prj.load(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "プロジェクトが見つかりません")


# ---------- プロジェクト ----------

@router.get("/projects")
def list_projects():
    return prj.list_projects()


@router.post("/projects")
async def create_project(request: Request):
    body = await request.json()
    return prj.create_project(str(body.get("name", ""))[:80])


@router.get("/projects/{project_id}")
def get_project(project_id: str):
    return prj.serialize(_load_or_404(project_id))


@router.delete("/projects/{project_id}")
def delete_project(project_id: str):
    _load_or_404(project_id)
    storage.delete_project(project_id)
    return {"ok": True}


# ---------- アップロード(§6.3) ----------

@router.post("/projects/{project_id}/assets/{role}/init")
async def asset_init(project_id: str, role: str, request: Request):
    if role not in prj.ROLES:
        raise HTTPException(400, f"不明なロール: {role}")
    body = await request.json()
    total = int(body["bytes"])
    if total <= 0:
        raise HTTPException(400, "bytes は正の値が必要です")
    with prj.update(project_id) as doc:
        existing = doc["assets"].get(role)
        if existing and existing.get("status") == "uploading" and existing.get("bytes") == total:
            return {"received": existing["received"]}   # レジューム
        doc["assets"][role] = {
            "path": f"assets/{role}.flac",
            "orig_name": str(body.get("filename", ""))[:120],
            "bytes": total,
            "received": 0,
            "status": "uploading",
        }
    upload_path = prj.project_dir(project_id) / "assets" / f"{role}.upload"
    upload_path.write_bytes(b"")
    return {"received": 0}


@router.put("/projects/{project_id}/assets/{role}/chunk")
async def asset_chunk(project_id: str, role: str, offset: int, request: Request):
    chunk = await request.body()
    _load_or_404(project_id)
    upload_path = prj.project_dir(project_id) / "assets" / f"{role}.upload"
    # 検証・書き込み・received 更新をひとつのロック内で行い、
    # seek+truncate で書き込みを冪等にする(部分書き込み・重複リトライ耐性)
    with prj.update(project_id) as doc:
        asset = doc["assets"].get(role)
        if not asset or asset.get("status") != "uploading":
            raise HTTPException(409, "init されていないかアップロード済みです")
        if offset != asset["received"]:
            raise HTTPException(409, detail={"received": asset["received"]})  # レジューム(§11)
        with open(upload_path, "r+b") as f:
            f.seek(offset)
            f.write(chunk)
            f.truncate(offset + len(chunk))
        asset["received"] = offset + len(chunk)
        complete = asset["received"] >= asset["bytes"]
        if complete:
            asset["status"] = "processing"
    if complete:
        threading.Thread(
            target=_finalize_asset, args=(project_id, role), daemon=True
        ).start()
    return {"received": offset + len(chunk), "complete": complete}


def _finalize_asset(project_id: str, role: str) -> None:
    """受信完了後: 原本の sha256(指紋の種)→ FLAC 化 → 原本破棄(§6.3)。"""
    pdir = prj.project_dir(project_id)
    upload_path = pdir / "assets" / f"{role}.upload"
    flac_path = pdir / "assets" / f"{role}.flac"
    try:
        digest = prj.sha256_file(upload_path)
        ffmpeg.run(["-i", str(upload_path), "-c:a", "flac", str(flac_path)])
        probe = ffmpeg.probe(flac_path)
        upload_path.unlink()
        with prj.update(project_id) as doc:
            doc["assets"][role].update(
                status="ready", sha256=digest, probe=probe,
                stored_bytes=flac_path.stat().st_size,
            )
    except Exception as exc:
        with prj.update(project_id) as doc:
            doc["assets"][role].update(status="failed", error=str(exc)[:500])


# ---------- ステージ ----------

@router.post("/projects/{project_id}/stages/{stage}/run")
async def run_stage(project_id: str, stage: str, request: Request):
    doc = _load_or_404(project_id)
    if stage not in doc["stage_order"]:
        raise HTTPException(400, f"不明なステージ: {stage}")
    body = await request.json() if int(request.headers.get("content-length") or 0) else {}
    try:
        runner.start(project_id, stage, body.get("params"))
    except runner.Busy as e:
        raise HTTPException(409, str(e))
    except runner.NotRunnable as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


@router.post("/projects/{project_id}/stages/{stage}/approve")
def approve_stage(project_id: str, stage: str):
    doc = _load_or_404(project_id)
    if stage not in doc["stage_order"]:
        raise HTTPException(400, f"不明なステージ: {stage}")
    try:
        runner.approve(project_id, stage)
    except runner.NotRunnable as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


_FILE_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def _stage_file(project_id: str, stage: str, prefix: str, name: str, suffix: str):
    doc = _load_or_404(project_id)
    if stage not in doc["stage_order"] or not _FILE_NAME_RE.match(name):
        raise HTTPException(404)
    path = storage.stage_dir(project_id, stage) / f"{prefix}_{name}{suffix}"
    if not path.exists():
        raise HTTPException(404)
    return path


@router.get("/projects/{project_id}/stages/{stage}/preview")
def stage_preview(project_id: str, stage: str, name: str):
    path = _stage_file(project_id, stage, "preview", name, ".opus")
    return FileResponse(path, media_type="audio/ogg")


@router.get("/projects/{project_id}/stages/{stage}/peaks")
def stage_peaks(project_id: str, stage: str, name: str):
    path = _stage_file(project_id, stage, "peaks", name, ".json")
    return FileResponse(path, media_type="application/json")


# ---------- 成果物 / 容量 ----------

@router.get("/projects/{project_id}/download/{artifact}")
def download(project_id: str, artifact: str):
    doc = _load_or_404(project_id)
    # ホワイトリスト = export ステージの report に載っている成果物のみ(R-11)
    artifacts = doc["stages"]["export"].get("report", {}).get("artifacts", {})
    entry = artifacts.get(artifact)
    if not entry:
        raise HTTPException(404)
    path = storage.stage_dir(project_id, "export") / entry["file"]
    if not path.exists():
        raise HTTPException(404, "成果物が見つかりません(再実行が必要)")
    return FileResponse(path, filename=f"{doc.get('name', project_id)}_{entry['file']}")


@router.get("/projects/{project_id}/usage")
def usage(project_id: str):
    _load_or_404(project_id)
    return {**storage.usage(project_id), "disk_free_bytes": storage.disk_free_bytes()}


@router.post("/projects/{project_id}/gc")
def gc(project_id: str, keep_recent: int = 0):
    _load_or_404(project_id)
    return storage.gc(project_id, keep_recent=keep_recent)
