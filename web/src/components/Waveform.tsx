import { useEffect, useRef, useState } from "react";

interface Peaks { peaks: number[]; duration: number; }

/** 事前計算済み peaks.json を Canvas に描画し、クリックでシークする。
 *  中間 FLAC はブラウザに送らない(§8)— 描画は peaks、再生は preview.opus。 */
export default function Waveform({
  peaksUrl, audio,
}: { peaksUrl: string; audio: HTMLAudioElement | null }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [data, setData] = useState<Peaks | null>(null);
  const [pos, setPos] = useState(0);

  useEffect(() => {
    let alive = true;
    fetch(peaksUrl).then(r => r.json()).then(d => alive && setData(d)).catch(() => {});
    return () => { alive = false; };
  }, [peaksUrl]);

  useEffect(() => {
    if (!audio) return;
    const onTime = () => setPos(audio.currentTime);
    audio.addEventListener("timeupdate", onTime);
    return () => audio.removeEventListener("timeupdate", onTime);
  }, [audio]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !data) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const g = canvas.getContext("2d")!;
    g.scale(dpr, dpr);
    g.clearRect(0, 0, w, h);
    const n = data.peaks.length;
    const mid = h / 2;
    const played = data.duration > 0 ? (pos / data.duration) * n : 0;
    for (let i = 0; i < n; i++) {
      const x = (i / n) * w;
      const amp = Math.max(1, data.peaks[i] * (h / 2 - 2));
      g.fillStyle = i < played ? "#4f8ef7" : "#57606c";
      g.fillRect(x, mid - amp, Math.max(1, w / n - 0.5), amp * 2);
    }
  }, [data, pos]);

  const seek = (e: React.MouseEvent) => {
    if (!audio || !data) return;
    const rect = canvasRef.current!.getBoundingClientRect();
    audio.currentTime = ((e.clientX - rect.left) / rect.width) * data.duration;
  };

  return <canvas ref={canvasRef} className="wave" onClick={seek} />;
}
