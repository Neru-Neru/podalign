"""ffmpeg/ffprobe ラッパ。

音の加工はすべて ffmpeg サブプロセスで行い、Python 側は
- raw f32le パイプで numpy に読み込む(解析用)
- ebur128 / loudnorm / astats の stderr 出力をパースする(測定用)
ことに徹する(設計 §4.2)。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import numpy as np

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# FLAC 24bit: ffmpeg の flac エンコーダは s32 入力を 24bit で書く
FLAC24 = ["-c:a", "flac", "-sample_fmt", "s32"]


class EnvironmentError_(RuntimeError):
    """起動時環境チェックの失敗(不足コンポーネントと導入方法を含む)。"""


def verify_environment() -> None:
    """ffmpeg/ffprobe の存在と librubberband(rubberband フィルタ)を確認する。

    ドリフト補正は rubberband が唯一の手段(atempo は ppm 級で無効 — 設計 §3)
    なので、無効ビルドのまま起動させず、ここで明確に案内して止める。
    """
    for tool in (FFMPEG, FFPROBE):
        if shutil.which(tool) is None:
            raise EnvironmentError_(
                f"'{tool}' が見つかりません。ffmpeg(librubberband 有効ビルド)を"
                "インストールしてください。Debian/Ubuntu: apt install ffmpeg / "
                "macOS: brew install ffmpeg / または同梱 Docker イメージを使用"
            )
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True
    )
    if "rubberband" not in proc.stdout:
        raise EnvironmentError_(
            "この ffmpeg には librubberband(rubberband フィルタ)がありません。"
            "クロックドリフト補正に必須です。librubberband 有効ビルドの ffmpeg "
            "(Debian/Ubuntu・Homebrew の標準パッケージは有効)を使うか、"
            "同梱 Docker イメージで起動してください。"
        )


class FFmpegError(RuntimeError):
    def __init__(self, cmd: list[str], stderr: str):
        self.cmd = cmd
        self.stderr = stderr
        tail = "\n".join(stderr.splitlines()[-15:])
        super().__init__(f"ffmpeg failed: {' '.join(cmd[:8])}...\n{tail}")


def run(args: list[str], timeout: int | None = None) -> str:
    """ffmpeg を実行し stderr テキストを返す(測定値のパース用)。"""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", *args]
    proc = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout
    )
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError(cmd, stderr)
    return stderr


def probe(path: str | Path) -> dict:
    """サンプルレート / チャンネル / 長さ / コーデックを取得する。"""
    cmd = [
        FFPROBE, "-hide_banner", "-v", "error",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,duration,start_time:format=duration,size",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.stderr.decode(errors="replace"))
    info = json.loads(proc.stdout)
    stream = next((s for s in info.get("streams", []) if "sample_rate" in s), {})
    fmt = info.get("format", {})
    duration = float(stream.get("duration") or fmt.get("duration") or 0.0)
    try:
        start_time = float(stream.get("start_time") or 0.0)
    except (TypeError, ValueError):
        start_time = 0.0
    return {
        "codec": stream.get("codec_name"),
        "sample_rate": int(stream.get("sample_rate", 0)),
        "channels": int(stream.get("channels", 0)),
        "duration": duration,
        "start_time": start_time,
        "bytes": int(fmt.get("size", 0)),
    }


def decode_f32(
    path: str | Path,
    rate: int,
    mono: bool = True,
    start: float | None = None,
    duration: float | None = None,
) -> np.ndarray:
    """raw float32 を stdout パイプで受け numpy 配列にする(§3 検証済み手法)。"""
    args = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error"]
    if start is not None:
        args += ["-ss", f"{start:.6f}"]
    args += ["-i", str(path)]
    if duration is not None:
        args += ["-t", f"{duration:.6f}"]
    args += ["-ar", str(rate)]
    if mono:
        args += ["-ac", "1"]
    args += ["-f", "f32le", "-"]
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise FFmpegError(args, proc.stderr.decode(errors="replace"))
    return np.frombuffer(proc.stdout, dtype=np.float32)


def stream_f32(
    path: str | Path, rate: int, mono: bool = True, chunk_samples: int = 1 << 20
) -> Iterator[np.ndarray]:
    """decode_f32 のストリーミング版。1時間素材でも RAM に載せずに舐める。"""
    args = [
        FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
        "-i", str(path), "-ar", str(rate),
    ]
    if mono:
        args += ["-ac", "1"]
    args += ["-f", "f32le", "-"]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(chunk_samples * 4)
            if not buf:
                break
            yield np.frombuffer(buf, dtype=np.float32)
    finally:
        proc.stdout.close()
        proc.wait()


_EBUR_I = re.compile(r"I:\s+(-?[\d.]+)\s+LUFS")
_EBUR_LRA = re.compile(r"LRA:\s+(-?[\d.]+)\s+LU")
_EBUR_PEAK = re.compile(r"Peak:\s+(-?[\d.]+)\s+dBFS")


def measure_ebur128(path: str | Path, extra_filters: str = "") -> dict:
    """ebur128 で Integrated / LRA / True Peak を測る。

    extra_filters を渡すと「そのフィルタを適用した後の」値を測れる
    (エンコードせずにチェーン結果を測定するために使う)。
    """
    af = (extra_filters + "," if extra_filters else "") + "ebur128=peak=true"
    stderr = run(["-i", str(path), "-af", af, "-f", "null", "-"])
    summary = stderr[stderr.rfind("Summary:"):]
    m_i, m_lra, m_tp = (
        _EBUR_I.search(summary),
        _EBUR_LRA.search(summary),
        _EBUR_PEAK.search(summary),
    )
    if not m_i:
        raise FFmpegError(["ebur128", str(path)], stderr)
    return {
        "I": float(m_i.group(1)),
        "LRA": float(m_lra.group(1)) if m_lra else None,
        "TP": float(m_tp.group(1)) if m_tp else None,
    }


def measure_loudnorm(path: str | Path, i: float, tp: float, lra: float) -> dict:
    """loudnorm 1パス目: 測定値 JSON を取得する(§7 Stage 5)。"""
    af = f"loudnorm=I={i}:TP={tp}:LRA={lra}:print_format=json"
    stderr = run(["-i", str(path), "-af", af, "-f", "null", "-"])
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1:
        raise FFmpegError(["loudnorm", str(path)], stderr)
    return json.loads(stderr[start : end + 1])


_ASTATS_FIELD = re.compile(r"^\[Parsed_astats.*\]\s+(.+?):\s+(-?[\d.]+|-inf|inf|nan)\s*$")


def measure_astats(path: str | Path) -> dict:
    """astats の Overall 統計(DC offset / peak level / peak count 等)。"""
    stderr = run(["-i", str(path), "-af", "astats=measure_perchannel=none", "-f", "null", "-"])
    out: dict[str, float] = {}
    for line in stderr.splitlines():
        m = _ASTATS_FIELD.match(line.strip())
        if m:
            try:
                out[m.group(1)] = float(m.group(2))
            except ValueError:
                pass
    return out
