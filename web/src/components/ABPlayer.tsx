import { useEffect, useRef, useState } from "react";
import Waveform from "./Waveform";

/** A/B 比較プレイヤー: 処理前/後を同じ再生位置のまま切り替える(§9 最優先)。
 *  単一の <audio> の src を差し替え、currentTime と再生状態を引き継ぐ。 */
export default function ABPlayer({
  label, processedUrl, originalUrl, peaksUrl,
}: {
  label: string;
  processedUrl: string;
  originalUrl?: string;   // 無ければ通常プレイヤー
  peaksUrl?: string;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [side, setSide] = useState<"A" | "B">("B");  // B = 処理後
  const [audioEl, setAudioEl] = useState<HTMLAudioElement | null>(null);

  useEffect(() => setAudioEl(audioRef.current), []);

  const switchTo = (target: "A" | "B") => {
    const audio = audioRef.current;
    if (!audio || target === side) return;
    const t = audio.currentTime;
    const playing = !audio.paused;
    audio.src = target === "B" ? processedUrl : originalUrl!;
    audio.currentTime = t;
    if (playing) void audio.play();
    setSide(target);
  };

  return (
    <div style={{ marginBottom: 12 }}>
      <div className="row" style={{ marginBottom: 4 }}>
        <strong style={{ fontSize: 13 }}>{label}</strong>
        {originalUrl && (
          <span>
            <button
              className={side === "A" ? "" : "ghost"}
              onClick={() => switchTo("A")}
            >A: 処理前</button>{" "}
            <button
              className={side === "B" ? "" : "ghost"}
              onClick={() => switchTo("B")}
            >B: 処理後</button>
          </span>
        )}
      </div>
      {peaksUrl && <Waveform peaksUrl={peaksUrl} audio={audioEl} />}
      <audio
        ref={audioRef} controls preload="none" src={processedUrl}
        style={{ width: "100%", height: 32, marginTop: 4 }}
      />
    </div>
  );
}
