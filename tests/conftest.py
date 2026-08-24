from __future__ import annotations

from pathlib import Path

import pytest

from app import project as prj


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(prj, "DATA_DIR", d)
    return d
