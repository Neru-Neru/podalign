import { useEffect, useState } from "react";
import { api, ProjectSummary } from "../api";

export default function ProjectList({ onOpen }: { onOpen: (id: string) => void }) {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  const reload = () => api.listProjects().then(setProjects).catch(e => setError(String(e)));
  useEffect(() => { reload(); }, []);

  const create = async () => {
    try {
      const p = await api.createProject(name.trim());
      onOpen(p.id);
    } catch (e) { setError(String(e)); }
  };

  const remove = async (id: string) => {
    if (!window.confirm("プロジェクトを完全に削除します。よろしいですか?")) return;
    await api.deleteProject(id);
    reload();
  };

  return (
    <div className="main" style={{ margin: "0 auto" }}>
      <h1>podalign</h1>
      <div className="card">
        <div className="row">
          <input
            placeholder="エピソード名 (例: ep-042)"
            value={name}
            onChange={e => setName(e.target.value)}
            onKeyDown={e => e.key === "Enter" && create()}
          />
          <button onClick={create}>新規プロジェクト</button>
        </div>
        {error && <p className="log">{error}</p>}
      </div>
      <div className="card">
        {projects.length === 0 && <p className="muted">プロジェクトはまだありません</p>}
        {projects.map(p => (
          <div key={p.id} className="proj-item">
            <span className="name" onClick={() => onOpen(p.id)}>{p.name}</span>
            <span className="muted">{p.created_at.slice(0, 10)}</span>
            <span className={`badge ${p.assets_ready ? "done" : "pending"}`}>
              {p.assets_ready ? "素材完備" : "素材待ち"}
            </span>
            <button className="ghost" onClick={() => remove(p.id)}>削除</button>
          </div>
        ))}
      </div>
    </div>
  );
}
