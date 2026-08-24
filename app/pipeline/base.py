"""Stage 共通インターフェース(設計 §7 冒頭)。

各ステージは `run(ctx, params) -> report` の純粋関数。入力は上流ステージの
成果物、出力は stages/<nn>_<name>/。決定性(N-5)を壊す要素(時刻・乱数)を
成果物に入れないこと。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import project as prj
from .. import storage
from . import analysis, ffmpeg

PREVIEW_BITRATE = "96k"
PREVIEW_BITRATE_CLEANUP = "128k"  # R-10: NR アーティファクト判定用に cleanup のみ高め


@dataclass
class StageContext:
    project_id: str
    stage: str
    doc: dict                                  # 実行開始時点のスナップショット
    progress: Callable[[str], None] = lambda msg: None

    @property
    def out_dir(self) -> Path:
        d = storage.stage_dir(self.project_id, self.stage)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def upstream_dir(self, stage: str) -> Path:
        return storage.stage_dir(self.project_id, stage)

    def asset(self, role: str) -> Path:
        return storage.asset_path(self.project_id, role, self.doc)

    # ---- 成果物ヘルパ ----

    def write_report(self, report: dict) -> None:
        (self.out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def make_preview(self, src: Path, name: str, bitrate: str = PREVIEW_BITRATE) -> None:
        """ブラウザ再生用 Opus(中間 FLAC はブラウザに送らない — §8)。"""
        self.progress(f"preview 生成: {name}")
        ffmpeg.run(
            ["-i", str(src), "-c:a", "libopus", "-b:a", bitrate,
             str(self.out_dir / f"preview_{name}.opus")]
        )

    def make_peaks(self, src: Path, name: str) -> None:
        self.progress(f"波形 peaks 生成: {name}")
        peaks = analysis.compute_peaks(src)
        payload = {"peaks": peaks, "duration": ffmpeg.probe(src)["duration"]}
        path = self.out_dir / f"peaks_{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")


SPEAKERS = ["speaker_a", "speaker_b", "speaker_c"]
