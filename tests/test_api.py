"""API のスモークテスト: プロジェクト作成 → チャンクアップロード(レジューム含む)。"""
from __future__ import annotations

import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app import project as prj


@pytest.fixture
def client(data_dir):
    from app.main import app
    return TestClient(app)


def _make_wav(path, seconds=1):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "pcm_s16le", str(path)],
        check=True,
    )


def test_project_lifecycle(client):
    created = client.post("/api/projects", json={"name": "ep-test"}).json()
    pid = created["id"]
    assert pid.startswith("p-")

    listed = client.get("/api/projects").json()
    assert any(p["id"] == pid for p in listed)

    doc = client.get(f"/api/projects/{pid}").json()
    assert doc["stages"]["sync"]["effective_status"] == "pending"

    assert client.delete(f"/api/projects/{pid}").json()["ok"]
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_chunk_upload_with_resume(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "up"}).json()["id"]
    wav = tmp_path / "in.wav"
    _make_wav(wav)
    data = wav.read_bytes()
    half = len(data) // 2

    r = client.post(f"/api/projects/{pid}/assets/speaker_a/init",
                    json={"bytes": len(data), "filename": "in.wav"})
    assert r.json()["received"] == 0

    r = client.put(f"/api/projects/{pid}/assets/speaker_a/chunk?offset=0",
                   content=data[:half])
    assert r.json() == {"received": half, "complete": False}

    # 間違ったオフセット → 409 で正しい続き位置が返る(レジューム §11)
    r = client.put(f"/api/projects/{pid}/assets/speaker_a/chunk?offset=0",
                   content=data[:half])
    assert r.status_code == 409
    assert r.json()["detail"]["received"] == half

    r = client.put(f"/api/projects/{pid}/assets/speaker_a/chunk?offset={half}",
                   content=data[half:])
    assert r.json()["complete"] is True

    # バックグラウンドの FLAC 化完了を待つ
    for _ in range(50):
        asset = client.get(f"/api/projects/{pid}").json()["assets"]["speaker_a"]
        if asset["status"] in ("ready", "failed"):
            break
        time.sleep(0.2)
    assert asset["status"] == "ready"
    assert asset["sha256"]
    assert (prj.project_dir(pid) / "assets" / "speaker_a.flac").exists()
    assert not (prj.project_dir(pid) / "assets" / "speaker_a.upload").exists()

    # 実行前提チェック: 素材が揃うまで run は 422
    r = client.post(f"/api/projects/{pid}/stages/ingest/run", json={})
    assert r.status_code == 422


def test_download_traversal_blocked(client):
    pid = client.post("/api/projects", json={"name": "sec"}).json()["id"]
    # ホワイトリスト(export report)に無い名前は 404
    assert client.get(f"/api/projects/{pid}/download/project_json").status_code == 404
    # トラバーサル試行で project.json の中身が返らないこと
    # (パスが崩れた場合 SPA フォールバックの index.html になるのは許容)
    r = client.get(f"/api/projects/{pid}/download/..%2Fproject.json")
    assert "stage_order" not in r.text
    assert client.get(f"/api/projects/{pid}/stages/sync/preview?name=..").status_code == 404
