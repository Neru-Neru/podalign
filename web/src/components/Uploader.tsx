import { useRef, useState } from "react";
import { AssetState, Project, ROLES, ROLE_LABELS, fmtBytes, uploadAsset } from "../api";

/** 素材のロール割り当てアップロード。チャンク進捗 + レジューム表示(§9 画面1)。 */
export default function Uploader({ project }: { project: Project }) {
  const [local, setLocal] = useState<Record<string, number>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const busy = useRef<Set<string>>(new Set());

  const onPick = async (role: string, file: File | undefined) => {
    if (!file || busy.current.has(role)) return;
    busy.current.add(role);
    setErrors(e => ({ ...e, [role]: "" }));
    try {
      await uploadAsset(project.id, role, file, received =>
        setLocal(l => ({ ...l, [role]: received / file.size })));
    } catch (e) {
      setErrors(er => ({ ...er, [role]: String(e) }));
    } finally {
      busy.current.delete(role);
    }
  };

  const statusText = (a: AssetState | undefined, role: string): string => {
    if (errors[role]) return "エラー";
    if (!a) return "未選択";
    switch (a.status) {
      case "uploading":
        return a.received > 0 && a.received < a.bytes && !busy.current.has(role)
          ? `中断 (${fmtBytes(a.received)} 受信済 — 同じファイルを選ぶと再開)`
          : `送信中 ${Math.floor((a.received / a.bytes) * 100)}%`;
      case "processing": return "FLAC 変換中…";
      case "ready": return `完了 (${fmtBytes(a.bytes)})`;
      case "failed": return `失敗: ${a.error ?? ""}`;
    }
  };

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>素材アップロード</h3>
      <p className="muted">話者A/B/Cのうち最低2人の録音と、リファレンス・ジングル・BGMを選択してください。3人目の録音は省略できます。</p>
      {ROLES.map(role => {
        const a = project.assets[role];
        const frac = a
          ? a.status === "ready" || a.status === "processing" ? 1
            : (local[role] ?? a.received / a.bytes)
          : 0;
        return (
          <div key={role}>
            <div className="upload-row">
              <span>{ROLE_LABELS[role]}</span>
              <div className={`bar ${a?.status === "ready" ? "done" : ""}`}>
                <div style={{ width: `${frac * 100}%` }} />
              </div>
              <span className="muted">{statusText(a, role)}</span>
            </div>
            {(!a || a.status !== "ready") && (
              <input
                type="file" accept="audio/*,.wav,.flac,.m4a,.mp3"
                style={{ marginBottom: 8 }}
                onChange={e => onPick(role, e.target.files?.[0])}
              />
            )}
            {errors[role] && <p className="log">{errors[role]}</p>}
          </div>
        );
      })}
      <p className="muted">
        アップロード後は自動で FLAC 化されます。切断しても同じファイルを選び直せば続きから再開します。
      </p>
    </div>
  );
}
