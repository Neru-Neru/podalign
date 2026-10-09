import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { api } from "../api";

interface Peaks { peaks: number[]; duration: number; }
interface Selection { start: number; end: number; }

const TRACKS = [
  ["speaker_a", "話者A", "#4f8ef7"],
  ["speaker_b", "話者B", "#3fb96f"],
  ["speaker_c", "話者C", "#e0a63c"],
  ["reference", "全体音声(リファレンス)", "#b77bea"],
] as const;
const SR = 48000;

function snap(value: number): number {
  return Math.round(value * SR) / SR;
}

/** 話者とリファレンスを同じ時間軸で描画し、範囲と左右ハンドルを編集するTrim専用ビュー。 */
export default function TrimView({
  projectId, speakers, params, programLengthSamples, onChange,
}: {
  projectId: string;
  speakers: string[];
  params: Record<string, any>;
  programLengthSamples: number;
  onChange: (start: number, end: number) => void;
}) {
  const speakerKey = speakers.join(",");
  const tracks = useMemo(
    () => TRACKS.filter(([name]) => name === "reference" || speakerKey.split(",").includes(name)),
    [speakerKey],
  );
  const programLengthS = programLengthSamples / SR;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<{ mode: "start" | "end" | "range"; anchor: number; base: Selection } | null>(null);
  const [data, setData] = useState<Record<string, Peaks>>({});
  const [width, setWidth] = useState(0);
  const [selection, setSelection] = useState<Selection>({
    start: Number(params.start_s ?? 0),
    end: Number(params.end_s ?? programLengthS),
  });

  useEffect(() => {
    const next = {
      start: Number(params.start_s ?? 0),
      end: Number(params.end_s ?? programLengthS),
    };
    setSelection(next);
  }, [params.start_s, params.end_s, programLengthS]);

  useEffect(() => {
    let alive = true;
    Promise.all(tracks.map(async ([name]) => {
      // 選択対象は常にSync直後の共通時間軸。Trim後の短い成果物を
      // 元の全長へ引き伸ばして描いてしまわないよう、ここでは読まない。
      const response = await fetch(api.peaksUrl(projectId, "sync", name));
      if (!response.ok) throw new Error("peaks unavailable");
      return [name, await response.json() as Peaks] as const;
    })).then(entries => {
      if (alive) setData(Object.fromEntries(entries));
    }).catch(() => { if (alive) setData({}); });
    return () => { alive = false; };
  }, [projectId, programLengthSamples, tracks]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const update = () => setWidth(canvas.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !width) return;
    const dpr = window.devicePixelRatio || 1;
    const height = 244;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    const g = canvas.getContext("2d")!;
    g.scale(dpr, dpr);
    g.clearRect(0, 0, width, height);
    const duration = Math.max(programLengthS, 1 / SR);
    const x = (time: number) => Math.max(0, Math.min(width, (time / duration) * width));
    const sx = x(selection.start);
    const ex = x(selection.end);
    const rowHeight = height / tracks.length;

    g.fillStyle = "#24282f";
    g.fillRect(0, 0, width, height);
    tracks.forEach(([name, label, color], row) => {
      const top = row * rowHeight;
      g.strokeStyle = "#3a3f47";
      g.beginPath(); g.moveTo(0, top); g.lineTo(width, top); g.stroke();
      g.fillStyle = "#9aa1ab";
      g.font = "12px system-ui";
      g.fillText(label, 8, top + 17);
      const peaks = data[name]?.peaks ?? [];
      if (peaks.length) {
        const mid = top + rowHeight * 0.62;
        const scale = rowHeight * 0.30;
        g.fillStyle = color;
        for (let i = 0; i < peaks.length; i++) {
          const px = (i / peaks.length) * width;
          const amp = Math.max(1, peaks[i] * scale);
          g.fillRect(px, mid - amp, Math.max(1, width / peaks.length - 0.5), amp * 2);
        }
      }
    });
    // 選択外を暗くし、共通選択範囲と左右ハンドルを全トラックに重ねる。
    g.fillStyle = "rgba(0, 0, 0, .48)";
    g.fillRect(0, 0, sx, height);
    g.fillRect(ex, 0, width - ex, height);
    g.fillStyle = "rgba(79, 142, 247, .13)";
    g.fillRect(sx, 0, Math.max(0, ex - sx), height);
    g.strokeStyle = "#4f8ef7";
    g.lineWidth = 2;
    [sx, ex].forEach(handle => {
      g.beginPath(); g.moveTo(handle, 0); g.lineTo(handle, height); g.stroke();
      g.fillStyle = "#4f8ef7";
      g.fillRect(handle - 5, height / 2 - 13, 10, 26);
    });
  }, [data, width, selection, programLengthS, tracks]);

  const timeAt = (clientX: number) => {
    const rect = canvasRef.current!.getBoundingClientRect();
    const fraction = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return snap(fraction * programLengthS);
  };

  const update = (next: Selection) => {
    const clamped = {
      start: Math.max(0, Math.min(programLengthS, snap(next.start))),
      end: Math.max(0, Math.min(programLengthS, snap(next.end))),
    };
    setSelection(clamped);
    onChange(clamped.start, clamped.end);
  };

  const pointerDown = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const t = timeAt(e.clientX);
    const handleDistance = Math.max(programLengthS / Math.max(width, 1) * 12, 0.02);
    const mode = Math.abs(t - selection.start) <= handleDistance
      ? "start" : Math.abs(t - selection.end) <= handleDistance
        ? "end" : "range";
    dragRef.current = { mode, anchor: t, base: selection };
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const pointerMove = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const t = timeAt(e.clientX);
    if (drag.mode === "start") update({ start: Math.min(t, selection.end), end: selection.end });
    else if (drag.mode === "end") update({ start: selection.start, end: Math.max(t, selection.start) });
    else {
      const delta = t - drag.anchor;
      const length = drag.base.end - drag.base.start;
      const start = Math.max(0, Math.min(programLengthS - length, drag.base.start + delta));
      update({ start, end: start + length });
    }
  };

  const pointerUp = () => { dragRef.current = null; };
  const setStart = (value: number) => update({ start: value, end: selection.end });
  const setEnd = (value: number) => update({ start: selection.start, end: value });

  return (
    <div className="trim-editor">
      <p className="muted">Sync直後の{tracks.length}本を同じ時間軸で表示しています。範囲をドラッグするか、左右のハンドルを動かしてから実行してください。</p>
      <canvas
        ref={canvasRef} className="trim-wave" onPointerDown={pointerDown}
        onPointerMove={pointerMove} onPointerUp={pointerUp} onPointerCancel={pointerUp}
        aria-label="Trim範囲選択波形"
      />
      <div className="trim-fields">
        <label>開始時刻 (秒)
          <input type="number" min="0" max={programLengthS} step={1 / SR}
            value={selection.start} onChange={e => setStart(Number(e.target.value))} />
        </label>
        <label>終了時刻 (秒)
          <input type="number" min="0" max={programLengthS} step={1 / SR}
            value={selection.end} onChange={e => setEnd(Number(e.target.value))} />
        </label>
        <span className="trim-length">選択長: {Math.max(0, selection.end - selection.start).toFixed(6)} 秒</span>
      </div>
    </div>
  );
}
