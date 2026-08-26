import { useRef, useState } from "react";
import { BrowserPipeline, ROLES, ROLE_LABELS } from "./browserPipeline";
import "./styles.css";

export default function App() {
  const [files, setFiles] = useState<Record<string, File>>({});
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState("素材を選択してください");
  const [download, setDownload] = useState<string | null>(null);
  const [error, setError] = useState("");
  const worker = useRef<Worker | null>(null);

  const choose = (role: string, file?: File) => {
    if (file) setFiles(prev => ({ ...prev, [role]: file }));
  };
  const ready = ROLES.every(role => files[role]);

  const run = async () => {
    if (!ready || running) return;
    setRunning(true); setError(""); setDownload(null);
    try {
      const pipeline = new BrowserPipeline(msg => setProgress(msg));
      worker.current = pipeline.worker;
      const blob = await pipeline.run(files as Record<string, File>);
      setDownload(URL.createObjectURL(blob));
      setProgress("完了。音声はこのブラウザ内だけで処理されました。");
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); setProgress("失敗"); }
    finally { worker.current?.terminate(); worker.current = null; setRunning(false); }
  };

  return <main className="main browser-app">
    <header><h1>podalign <small>ブラウザ版</small></h1>
      <p>音声ファイルは外部サーバーへ送信されません。重い処理は Web Worker で実行します。</p></header>
    <section className="card"><h2>素材</h2>
      {ROLES.map(role => <label className="file-row" key={role}><span>{ROLE_LABELS[role]}</span>
        <input type="file" accept="audio/*" disabled={running} onChange={e => choose(role, e.target.files?.[0])}/>
        <span className="muted">{files[role]?.name ?? "未選択"}</span>
      </label>)}
      <button disabled={!ready || running} onClick={run}>{running ? "処理中…" : "ブラウザで処理"}</button>
      <button className="ghost" disabled={!running} onClick={() => worker.current?.postMessage({ type: "cancel" })}>キャンセル</button>
    </section>
    <section className="card"><h2>進捗</h2><progress max={100} value={progress.match(/(\\d+)%/)?.[1] ? Number(progress.match(/(\\d+)%/)![1]) : undefined}/><p>{progress}</p>{error && <p className="log">{error}</p>}
      {download && <a className="download" download="podalign-browser.wav" href={download}>完成した WAV を保存</a>}
    </section>
    <section className="card note"><h2>処理内容</h2><p>同期（offset / drift 推定）、ドリフト補正、ノイズ低減、ダイナミクス、BGM ducking、-16 LUFS 相当の正規化、WAV export、QC を Worker 内で順番に実行します。</p><p>メモリを抑えるため、WorkerがPCM WAVをFile.slice()で30秒windowずつ読み、話者ごとの中間結果と最終mixをOPFSへ逐次書き出します。現在はブラウザ標準APIだけでbounded decodeを実現するため、入力はPCM WAV（16/24/32bit）に限定しています。</p></section>
  </main>;
}
