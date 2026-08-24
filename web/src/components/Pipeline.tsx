import { useCallback, useEffect, useRef, useState } from "react";
import { Project, api, fmtBytes } from "../api";
import StagePanel from "./StagePanel";
import Uploader from "./Uploader";

/** 画面2: パイプライン(メイン)。進捗は1秒ポーリング(§4.2)。 */
export default function Pipeline({
  projectId, onBack,
}: { projectId: string; onBack: () => void }) {
  const [project, setProject] = useState<Project | null>(null);
  const [usage, setUsage] = useState<{ total_bytes: number; disk_free_bytes: number } | null>(null);
  const [error, setError] = useState("");
  const timer = useRef<number>();

  const reload = useCallback(async () => {
    try {
      setProject(await api.getProject(projectId));
      setError("");
    } catch (e) { setError(String(e)); }
  }, [projectId]);

  useEffect(() => {
    reload();
    timer.current = window.setInterval(reload, 1000);
    return () => window.clearInterval(timer.current);
  }, [reload]);

  useEffect(() => {
    api.usage(projectId).then(setUsage).catch(() => {});
    const t = window.setInterval(
      () => api.usage(projectId).then(setUsage).catch(() => {}), 10000);
    return () => window.clearInterval(t);
  }, [projectId]);

  const purge = async () => {
    if (!window.confirm("中間成果物を破棄します(必要になれば自動で再生成されます)。よろしいですか?")) return;
    await api.gc(projectId);
    api.usage(projectId).then(setUsage).catch(() => {});
  };

  if (!project) {
    return <div className="main">{error ? <p className="log">{error}</p> : "読み込み中…"}</div>;
  }

  const anyRunning = project.stage_order.some(
    s => project.stages[s].status === "running");
  const assetsReady = Object.keys(project.assets).length >= 6 &&
    Object.values(project.assets).every(a => a.status === "ready");

  return (
    <div className="layout">
      <div className="main">
        <div className="row" style={{ marginBottom: 10 }}>
          <a href="#" onClick={e => { e.preventDefault(); onBack(); }}>← 一覧</a>
          <h1 style={{ margin: 0 }}>{project.name}</h1>
        </div>
        {!assetsReady && <Uploader project={project} />}
        {project.stage_order.map(stage => (
          <StagePanel
            key={stage}
            project={project}
            stage={stage}
            anyRunning={anyRunning}
            onAction={reload}
          />
        ))}
      </div>
      <div className="sidebar">
        <div className="section-title">プロジェクト容量</div>
        {usage && (
          <>
            <p style={{ fontSize: 22, margin: "4px 0" }}>{fmtBytes(usage.total_bytes)}</p>
            <p className="muted">ディスク空き {fmtBytes(usage.disk_free_bytes)}</p>
          </>
        )}
        <button className="ghost" onClick={purge}>中間成果物を破棄</button>
        <div className="section-title" style={{ marginTop: 24 }}>素材</div>
        {Object.entries(project.assets).map(([role, a]) => (
          <p key={role} className="muted" style={{ margin: "3px 0" }}>
            {role}: {a.status === "ready" ? fmtBytes(a.bytes) : a.status}
          </p>
        ))}
        {Object.keys(project.assets).length === 0 && (
          <p className="muted">アップロード待ち</p>
        )}
      </div>
    </div>
  );
}
