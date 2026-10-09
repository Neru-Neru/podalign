"""project.json の read/write(atomic) + 指紋 + stale 判定。

- 書き込みは tmp+rename でアトミック(クラッシュ耐性)
- read-modify-write はプロジェクト単位の threading.Lock 内でのみ行う
  (並行アップロード・ステージ完了・承認の競合で更新が巻き戻るのを防ぐ。
   ステージ実行はワーカースレッドで走るため asyncio.Lock ではなく threading.Lock)
- 指紋は再帰定義: fp(stage) = sha256(fp(upstream) + canonical_json(params))。
  ingest のみ原素材バイトの sha256 を種にする。成果物バイトは含めない
  (GC 後も計算可能。N-5 の決定性が前提)
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("PODCAST_DATA_DIR", "data"))

STAGE_ORDER = ["ingest", "sync", "trim", "cleanup", "dynamics", "mix", "master", "export"]
STAGE_DIRS = {name: f"{i:02d}_{name}" for i, name in enumerate(STAGE_ORDER)}
ROLES = ["speaker_a", "speaker_b", "speaker_c", "reference", "jingle", "bgm"]

_BUILTIN_PARAMS: dict[str, dict] = {
    "ingest": {},
    "sync": {"drift_threshold_ppm": 5.0, "n_segments": 10, "segment_s": 30.0},
    # end_s は Sync の program_length_samples を意味する動的既定値。Sync 実行前には
    # 長さが分からないため null で保存し、Trim 実行時に解決する。
    "trim": {"start_s": 0.0, "end_s": None},
    "cleanup": {
        "highpass_hz": 80,
        "nr_max_db": 12.0,
        "gate_range_db": -12.0,
        "target_lufs": -20.0,
        "deess_intensity": 0.3,
    },
    "dynamics": {
        "comp1": {"threshold_db": -20, "ratio": 2.5, "attack_ms": 10, "release_ms": 150},
        "comp2": {"threshold_db": -12, "ratio": 6, "attack_ms": 2, "release_ms": 60},
        "eq_enabled": True,
        "eq_presence_db": 2.0,
        "eq_mud_db": -2.0,
    },
    "mix": {
        "pan_width": 0.3,
        "premix_gain_db": -7.0,
        "bgm_bed_db": -24.0,
        "duck_threshold_db": -30.0,
        "duck_ratio": 8.0,
        # None は他の系統に追従する自動開始。数値は番組先頭からの絶対時刻。
        "jingle_start_s": 0.0,
        "voice_start_s": None,
        "bgm_start_s": None,
        "bgm_fade_in_s": 2.0,
        "bgm_loop_crossfade_s": 2.0,
    },
    "master": {"target_i": -16.0, "target_tp": -1.5, "target_lra": 11.0},
    "export": {"title": "", "artist": "", "album": ""},
}

def _load_default_params() -> dict[str, dict]:
    """presets/default.json があれば既定パラメータを上書きする(YAML 依存を避け JSON)。"""
    preset = Path(__file__).resolve().parent.parent / "presets" / "default.json"
    params = json.loads(json.dumps(_BUILTIN_PARAMS))
    if preset.exists():
        try:
            for stage, p in json.loads(preset.read_text(encoding="utf-8")).items():
                if stage in params:
                    params[stage].update(p)
        except (json.JSONDecodeError, AttributeError):
            pass
    return params


DEFAULT_PARAMS = _load_default_params()

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def project_dir(project_id: str) -> Path:
    return DATA_DIR / project_id


def _lock_for(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_project(name: str) -> dict:
    # R-11/R-12: ユーザー入力をパスにしない。ID はサーバ生成
    project_id = "p-" + uuid.uuid4().hex[:12]
    pdir = project_dir(project_id)
    (pdir / "assets").mkdir(parents=True)
    (pdir / "stages").mkdir()
    doc = {
        "id": project_id,
        "name": name or project_id,
        "created_at": now_iso(),
        "assets": {},
        "stage_order": list(STAGE_ORDER),
        "stages": {
            s: {
                # プロジェクトごとの編集が他の新規プロジェクトやテストへ
                # 漏れないよう、既定値を参照共有しない。
                "params": json.loads(json.dumps(DEFAULT_PARAMS[s])),
                "input_fingerprint": None,
                "status": "pending",
                "artifacts_evicted": False,
                "started_at": None,
                "finished_at": None,
                "progress": "",
                "log_tail": [],
                "report": {},
            }
            for s in STAGE_ORDER
        },
    }
    _write(project_id, doc)
    return doc


def load(project_id: str) -> dict:
    path = project_dir(project_id) / "project.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(project_id: str, doc: dict) -> None:
    path = project_dir(project_id) / "project.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@contextmanager
def update(project_id: str):
    """ロック下で read-modify-write。`with update(pid) as doc: doc[...] = ...`"""
    with _lock_for(project_id):
        doc = load(project_id)
        yield doc
        _write(project_id, doc)


def list_projects() -> list[dict]:
    out = []
    if not DATA_DIR.exists():
        return out
    for p in sorted(DATA_DIR.iterdir()):
        if (p / "project.json").exists():
            doc = json.loads((p / "project.json").read_text(encoding="utf-8"))
            out.append(
                {
                    "id": doc["id"],
                    "name": doc.get("name", doc["id"]),
                    "created_at": doc["created_at"],
                    "assets_ready": assets_ready(doc),
                }
            )
    return out


# ---------- 指紋 / stale ----------

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_fingerprints(doc: dict) -> dict[str, str | None]:
    """現在の assets + params から各ステージの「あるべき指紋」を再帰計算する。

    原素材が揃っていない間は None(実行不可)。
    """
    fps: dict[str, str | None] = {}
    if not assets_ready(doc):
        return {s: None for s in doc["stage_order"]}
    seed = canonical_json(
        {role: doc["assets"][role]["sha256"] for role in ROLES}
    )
    prev: str | None = None
    for stage in doc["stage_order"]:
        params = canonical_json(doc["stages"][stage]["params"])
        base = seed if prev is None else prev
        prev = hashlib.sha256(f"{stage}|{base}|{params}".encode()).hexdigest()
        fps[stage] = "sha256:" + prev
    return fps


def assets_ready(doc: dict) -> bool:
    return all(
        doc["assets"].get(r, {}).get("status") == "ready" for r in ROLES
    )


def effective_status(doc: dict) -> dict[str, str]:
    """保存済み指紋と期待指紋を突き合わせ、stale を注釈した状態を返す。

    done/approved でも上流やパラメータが変わっていれば stale(§5.3)。
    """
    fps = expected_fingerprints(doc)
    out = {}
    for stage in doc["stage_order"]:
        st = doc["stages"][stage]
        status = st["status"]
        if status in ("done", "approved") and st["input_fingerprint"] != fps[stage]:
            status = "stale"
        out[stage] = status
    return out


def serialize(doc: dict) -> dict:
    """GET /api/projects/{id} 用: stale 注釈と期待指紋を付けてそのまま返す。"""
    doc = json.loads(json.dumps(doc))  # deep copy
    statuses = effective_status(doc)
    for stage, status in statuses.items():
        doc["stages"][stage]["effective_status"] = status
    return doc
