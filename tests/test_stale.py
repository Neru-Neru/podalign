"""stale 判定の検証(設計 §12.4)。

Stage 3 (cleanup) のパラメータを変更したとき、cleanup 以降が stale になり
Stage 0/1/2 (ingest/sync/trim) は影響を受けないこと。指紋は再帰定義なので
GC で成果物が消えていても判定できること(R-1)。
"""
from __future__ import annotations

from app import project as prj


def _make_ready_project() -> dict:
    doc = prj.create_project("test")
    for role in prj.ROLES:
        doc["assets"][role] = {
            "path": f"assets/{role}.flac", "bytes": 100, "received": 100,
            "status": "ready", "sha256": f"fake-{role}",
        }
    fps = prj.expected_fingerprints(doc)
    for stage in doc["stage_order"]:
        doc["stages"][stage]["status"] = "approved"
        doc["stages"][stage]["input_fingerprint"] = fps[stage]
    return doc


def test_param_change_stales_downstream_only(data_dir):
    doc = _make_ready_project()
    assert all(s == "approved" for s in prj.effective_status(doc).values())

    doc["stages"]["cleanup"]["params"]["nr_max_db"] = 9.0
    statuses = prj.effective_status(doc)
    assert statuses["ingest"] == "approved"
    assert statuses["sync"] == "approved"
    assert statuses["trim"] == "approved"
    for stage in ("cleanup", "dynamics", "mix", "master", "export"):
        assert statuses[stage] == "stale", stage


def test_asset_change_stales_everything(data_dir):
    doc = _make_ready_project()
    doc["assets"]["speaker_b"]["sha256"] = "different"
    statuses = prj.effective_status(doc)
    assert all(s == "stale" for s in statuses.values())


def test_trim_param_change_stales_trim_and_downstream_only(data_dir):
    doc = _make_ready_project()
    doc["stages"]["trim"]["params"]["start_s"] = 1.0
    statuses = prj.effective_status(doc)
    assert statuses["ingest"] == "approved"
    assert statuses["sync"] == "approved"
    assert statuses["trim"] == "stale"
    for stage in ("cleanup", "dynamics", "mix", "master", "export"):
        assert statuses[stage] == "stale", stage


def test_fingerprint_independent_of_artifacts(data_dir):
    """指紋は成果物バイトに依存しない = GC 後も同じ値が計算できる(R-1)。"""
    doc = _make_ready_project()
    before = prj.expected_fingerprints(doc)
    for stage in doc["stage_order"]:
        doc["stages"][stage]["artifacts_evicted"] = True
    assert prj.expected_fingerprints(doc) == before


def test_params_canonicalization(data_dir):
    """キー順が違うだけの params は同じ指紋になる。"""
    doc = _make_ready_project()
    p = doc["stages"]["sync"]["params"]
    doc["stages"]["sync"]["params"] = dict(reversed(list(p.items())))
    assert all(s == "approved" for s in prj.effective_status(doc).values())
