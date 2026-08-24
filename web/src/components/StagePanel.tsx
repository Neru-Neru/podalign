import { useEffect, useState } from "react";
import { Project, StageState, api, STAGE_LABELS } from "../api";
import ABPlayer from "./ABPlayer";
import SyncView from "./SyncView";

const SPEAKERS = ["speaker_a", "speaker_b", "speaker_c"];

/** ステージごとの試聴トラックと A/B 比較先(処理前 = 上流ステージの同トラック) */
function tracksFor(stage: string): { name: string; ab?: { stage: string; name: string }; peaks?: boolean }[] {
  switch (stage) {
    case "ingest":
      return ["speaker_a", "speaker_b", "speaker_c", "reference", "jingle", "bgm"]
        .map(name => ({ name, peaks: true }));
    case "sync":
      return [
        { name: "mix_check", peaks: false },
        ...SPEAKERS.map(name => ({ name, ab: { stage: "ingest", name }, peaks: true })),
      ];
    case "cleanup":
      return SPEAKERS.map(name => ({ name, ab: { stage: "sync", name }, peaks: true }));
    case "dynamics":
      return SPEAKERS.map(name => ({ name, ab: { stage: "cleanup", name }, peaks: true }));
    case "mix":
      return [{ name: "mix", peaks: true }];
    case "master":
      return [{ name: "master", ab: { stage: "mix", name: "mix" }, peaks: true }];
    default:
      return [];
  }
}

const STATUS_LABELS: Record<string, string> = {
  pending: "未実行", running: "実行中", done: "完了(承認待ち)",
  failed: "失敗", approved: "承認済み", stale: "要再実行(上流が変更)",
};

/** ネストした params を再帰的に number/checkbox/text 入力へ展開する汎用エディタ */
function ParamFields({
  value, path, onChange,
}: { value: Record<string, any>; path: string[]; onChange: (path: string[], v: unknown) => void }) {
  return (
    <>
      {Object.entries(value).map(([key, v]) => {
        const p = [...path, key];
        const label = path.length ? `${path.join(".")}.${key}` : key;
        if (v !== null && typeof v === "object") {
          return <ParamFields key={key} value={v} path={p} onChange={onChange} />;
        }
        if (typeof v === "boolean") {
          return (
            <label key={key}>{label}
              <input type="checkbox" checked={v}
                onChange={e => onChange(p, e.target.checked)} />
            </label>
          );
        }
        return (
          <label key={key}>{label}
            <input
              type={typeof v === "number" ? "number" : "text"}
              step="any" defaultValue={String(v)}
              onChange={e => onChange(
                p, typeof v === "number" ? Number(e.target.value) : e.target.value)}
            />
          </label>
        );
      })}
    </>
  );
}

function ReportTable({ rows }: { rows: [string, unknown][] }) {
  return (
    <div className="report">
      <table><tbody>
        {rows.map(([k, v]) => (
          <tr key={k}><th>{k}</th><td>{
            typeof v === "number" ? v : typeof v === "string" ? v : JSON.stringify(v)
          }</td></tr>
        ))}
      </tbody></table>
    </div>
  );
}

function StageReport({ stage, state }: { stage: string; state: StageState }) {
  const r = state.report;
  if (!r || Object.keys(r).length === 0) return null;
  if (stage === "ingest") {
    return (
      <div className="report">
        <table>
          <thead><tr>
            <th>トラック</th><th>長さ</th><th>ピーク</th><th>クリップ率</th><th>無音率</th>
          </tr></thead>
          <tbody>
            {Object.entries(r.tracks ?? {}).map(([role, t]: [string, any]) => (
              <tr key={role}>
                <td>{role}</td>
                <td>{Math.round(t.duration_s / 60)}分{Math.round(t.duration_s % 60)}秒</td>
                <td>{t.peak_db?.toFixed?.(1)} dB</td>
                <td>{(t.clip_ratio * 100).toFixed(3)}%</td>
                <td>{(t.silence_ratio * 100).toFixed(0)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  if (stage === "sync") return <SyncView stage={state} />;
  if (stage === "cleanup") {
    return (
      <div className="report">
        <table>
          <thead><tr>
            <th>トラック</th><th>ノイズフロア</th><th>ハム</th><th>NR量</th><th>整合ゲイン</th>
          </tr></thead>
          <tbody>
            {Object.entries(r.tracks ?? {}).map(([role, t]: [string, any]) => (
              <tr key={role}>
                <td>{role}</td>
                <td>{t.noise_floor_db} dB</td>
                <td>{t.hum_hz ? `${t.hum_hz}Hz 除去` : "なし"}</td>
                <td>{t.nr_db} dB</td>
                <td>{t.align_gain_db > 0 ? "+" : ""}{t.align_gain_db} dB</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  if (stage === "mix") {
    return <ReportTable rows={[
      ["話者バス", `${r.bus_lufs} LUFS`],
      ["BGM ゲイン", `${r.bgm_gain_db} dB (${r.bgm_looped ? "ループ" : "1回"})`],
      ["ジングルゲイン", `${r.jingle_gain_db} dB`],
      ["本編長", `${Math.round(r.program_length_s / 60)}分${Math.round(r.program_length_s % 60)}秒`],
    ]} />;
  }
  if (stage === "master") {
    const f = r.final ?? {};
    const t = r.target ?? {};
    return <ReportTable rows={[
      ["Integrated", `${f.I} LUFS (目標 ${t.I} ±0.5)`],
      ["LRA", `${f.LRA} LU`],
      ["True Peak", `${f.TP} dBTP (上限 -1.0)`],
      ["処理モード", r.normalization_type ?? "-"],
    ]} />;
  }
  return null;
}

export default function StagePanel({
  project, stage, anyRunning, onAction,
}: {
  project: Project;
  stage: string;
  anyRunning: boolean;
  onAction: () => void;
}) {
  const state = project.stages[stage];
  const status = state.effective_status;
  const [open, setOpen] = useState(status !== "approved");
  const [params, setParams] = useState<Record<string, any>>(state.params);
  const [error, setError] = useState("");

  // 承認されたら自動で折りたたむ(手動トグルは維持)
  useEffect(() => { setOpen(status !== "approved"); }, [status]);
  useEffect(() => { setParams(state.params); }, [state.params]);

  const upstreamApproved = project.stage_order
    .slice(0, project.stage_order.indexOf(stage))
    .every(s => project.stages[s].effective_status === "approved");
  const assetsReady = Object.keys(project.assets).length >= 6 &&
    Object.values(project.assets).every(a => a.status === "ready");
  const canRun = assetsReady && upstreamApproved && !anyRunning && status !== "running";

  const setParam = (path: string[], v: unknown) => {
    setParams(prev => {
      const next = structuredClone(prev);
      let node: any = next;
      for (const k of path.slice(0, -1)) node = node[k];
      node[path[path.length - 1]] = v;
      return next;
    });
  };

  const run = async () => {
    setError("");
    try { await api.runStage(project.id, stage, params); onAction(); }
    catch (e) { setError(String(e)); }
  };
  const approve = async () => {
    setError("");
    try { await api.approveStage(project.id, stage); onAction(); }
    catch (e) { setError(String(e)); }
  };

  return (
    <div className={`card ${open ? "" : "collapsed"}`}>
      <div className="stage-head" onClick={() => setOpen(o => !o)}>
        <h3>{STAGE_LABELS[stage]}</h3>
        {status === "running" && state.progress && (
          <span className="progress-msg">{state.progress}</span>
        )}
        <span className={`badge ${status}`}>{STATUS_LABELS[status]}</span>
      </div>
      {open && (
        <div onClick={e => e.stopPropagation()}>
          {Object.keys(params).length > 0 && (
            <div className="params">
              <ParamFields value={params} path={[]} onChange={setParam} />
            </div>
          )}
          <div className="row">
            <button onClick={run} disabled={!canRun}>
              {status === "done" || status === "approved" || status === "stale"
                ? "再実行" : "実行"}
            </button>
            {status === "done" && (
              <button onClick={approve} style={{ background: "var(--ok)" }}>
                承認して次へ
              </button>
            )}
            {!upstreamApproved && status === "pending" && (
              <span className="muted">上流ステージの承認待ち</span>
            )}
          </div>
          {error && <p className="log">{error}</p>}
          {status === "failed" && state.log_tail.length > 0 && (
            <p className="log">{state.log_tail.join("\n")}</p>
          )}
          {(status === "done" || status === "approved" || status === "stale") && (
            <>
              <StageReport stage={stage} state={state} />
              {(state.report?.warnings ?? []).length > 0 && (
                <ul className="warn-list">
                  {state.report.warnings.map((w: string, i: number) => <li key={i}>{w}</li>)}
                </ul>
              )}
              {stage === "export" ? (
                <div className="row" style={{ marginTop: 10 }}>
                  {Object.entries(state.report?.artifacts ?? {}).map(([fmt, a]: [string, any]) => (
                    <a key={fmt} href={api.downloadUrl(project.id, fmt)} download>
                      {a.file}
                    </a>
                  ))}
                </div>
              ) : (
                <div style={{ marginTop: 12 }}>
                  {tracksFor(stage).map(t => (
                    <ABPlayer
                      key={t.name}
                      label={t.name}
                      processedUrl={api.previewUrl(project.id, stage, t.name)}
                      originalUrl={t.ab
                        ? api.previewUrl(project.id, t.ab.stage, t.ab.name)
                        : undefined}
                      peaksUrl={t.peaks
                        ? api.peaksUrl(project.id, stage, t.name)
                        : undefined}
                    />
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
