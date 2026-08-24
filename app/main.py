"""FastAPI エントリポイント: API ルーティング + Vite ビルド成果物の static 配信。

起動: uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import project as prj
from .api import router
from .pipeline import ffmpeg

# 起動時環境チェック: librubberband 無効の ffmpeg はセルフホスト最頻出の
# つまずきなので、リクエストを受ける前に明確なエラーで止める
ffmpeg.verify_environment()

app = FastAPI(title="podalign")
app.include_router(router)

prj.DATA_DIR.mkdir(parents=True, exist_ok=True)

_web_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
if _web_dist.exists():
    app.mount("/assets", StaticFiles(directory=_web_dist / "assets"), name="static")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        # SPA fallback: API 以外は index.html
        candidate = _web_dist / path
        if path and candidate.is_file() and candidate.resolve().is_relative_to(_web_dist):
            return FileResponse(candidate)
        return FileResponse(_web_dist / "index.html")
