import { useEffect, useState } from "react";
import { Project, StageState, api, STAGE_LABELS, speakerRoles } from "../api";
import ABPlayer from "./ABPlayer";
import SyncView from "./SyncView";
import TrimView from "./TrimView";

const LEGACY_STAGE_LABELS: Record<string, string> = {
  cleanup: "2. Cleanup(整音)", dynamics: "3. Dynamics(コンプ・EQ)",
  mix: "4. Mix", master: "5. Master", export: "6. Export",
};

function stageLabel(stage: string, project: Project): string {
  return !project.stage_order.includes("trim")
    ? (LEGACY_STAGE_LABELS[stage] ?? STAGE_LABELS[stage])
    : STAGE_LABELS[stage];
}

/** ステージごとの試聴トラックと A/B 比較先(処理前 = 上流ステージの同トラック) */
function tracksFor(stage: string, project: Project): { name: string; ab?: { stage: string; name: string }; peaks?: boolean }[] {
  const speakers = speakerRoles(project);
  switch (stage) {
    case "ingest":
      return [...speakers, "reference", "jingle", "bgm"]
        .map(name => ({ name, peaks: true }));
    case "sync":
      return [
        { name: "mix_check", peaks: false },
        ...speakers.map(name => ({ name, ab: { stage: "ingest", name }, peaks: true })),
      ];
    case "trim":
      return [...speakers, "reference"]
        .map(name => ({
          name,
          ab: { stage: name === "reference" ? "ingest" : "sync", name },
          peaks: true,
        }));
    case "cleanup":
      return speakers.map(name => ({
        name,
        ab: { stage: project.stage_order.includes("trim") ? "trim" : "sync", name },
        peaks: true,
      }));
    case "dynamics":
      return speakers.map(name => ({ name, ab: { stage: "cleanup", name }, peaks: true }));
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

const PARAM_LABELS: Record<string, string> = {
  drift_threshold_ppm: "ドリフト補正のしきい値 (ppm)",
  n_segments: "解析区間数",
  segment_s: "解析区間の長さ (秒)",
  highpass_hz: "低域カット周波数 (Hz)",
  nr_max_db: "ノイズ除去の最大量 (dB)",
  gate_range_db: "ゲートで下げる音量 (dB)",
  target_lufs: "目標音量 (LUFS)",
  deess_intensity: "歯擦音（サ行）の抑制強度",
  comp1: "コンプレッサー1",
  comp2: "コンプレッサー2",
  threshold_db: "圧縮開始のしきい値 (dB)",
  ratio: "圧縮比",
  attack_ms: "圧縮がかかる速さ (ms)",
  release_ms: "圧縮が戻る時間 (ms)",
  eq_enabled: "音質調整（EQ）を有効にする",
  eq_presence_db: "声の明瞭さの調整 (4 kHz / dB)",
  eq_mud_db: "低域のこもりの調整 (250 Hz / dB)",
  pan_width: "話者の左右の広がり",
  premix_gain_db: "ミックス前の音量調整 (dB)",
  bgm_bed_db: "BGMの基本音量 (dB)",
  duck_threshold_db: "BGMを下げ始める声のしきい値 (dB)",
  duck_ratio: "会話中にBGMを下げる圧縮比",
  bgm_loop_crossfade_s: "BGMループのつなぎ時間 (秒)",
  target_i: "目標音量 (LUFS)",
  target_tp: "最大ピークの目標 (dBTP)",
  target_lra: "音量の変動幅の目標 (LU)",
  title: "タイトル",
  artist: "アーティスト",
  album: "アルバム",
};

/** ネストした params を再帰的に number/checkbox/text 入力へ展開する汎用エディタ */
function ParamFields({
  value, path, onChange,
}: { value: Record<string, any>; path: string[]; onChange: (path: string[], v: unknown) => void }) {
  return (
    <>
      {Object.entries(value).map(([key, v]) => {
        const p = [...path, key];
        const label = p.map(part => PARAM_LABELS[part] ?? part).join(" / ");
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

function seconds(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value} 秒` : "-";
}

/** Mix の開始位置は null を自動追従として扱うため、汎用入力とは分けて表示する。 */
function MixFields({
  params, jingleDuration, onChange,
}: {
  params: Record<string, any>;
  jingleDuration: number;
  onChange: (path: string[], v: unknown) => void;
}) {
  const jingleStart = Number(params.jingle_start_s ?? 0);
  const resolvedVoice = params.voice_start_s == null ? jingleStart + jingleDuration : Number(params.voice_start_s);
  const resolvedBgm = params.bgm_start_s == null ? resolvedVoice + 10 : Number(params.bgm_start_s);
  const startField = (key: string, title: string, automatic: string, resolved: number) => {
    const manual = params[key] != null;
    return (
      <div className="mix-timing-row" key={key}>
        <label>{title}
          <input type="number" min="0" step="0.1" disabled={!manual}
            value={manual ? params[key] : ""}
            placeholder={automatic}
            onChange={e => onChange([key], Number(e.target.value))} />
        </label>
        <button type="button" className="small" onClick={() => onChange([key], manual ? null : resolved)}>
          {manual ? "自動に戻す" : "手動指定"}
        </button>
        <span className="muted">{manual ? `指定: ${seconds(resolved)}` : automatic}</span>
      </div>
    );
  };
  return (
    <div className="mix-fields">
      <h4>開始タイミング</h4>
      <div className="mix-timing-row">
        <label>ジングル開始 (秒)
          <input type="number" min="0" step="0.1" value={jingleStart}
            onChange={e => onChange(["jingle_start_s"], Number(e.target.value))} />
        </label>
        <span className="muted">番組先頭から {seconds(jingleStart)}</span>
      </div>
      {startField("voice_start_s", "話者開始 (秒)", "自動: ジングル終了時", resolvedVoice)}
      {startField("bgm_start_s", "BGM開始 (秒)", "自動: 話者開始から10秒後", resolvedBgm)}
      <div className="mix-timing-row">
        <label>BGM フェードイン (秒)
          <input type="number" min="0" step="0.1" value={params.bgm_fade_in_s ?? 2}
            onChange={e => onChange(["bgm_fade_in_s"], Number(e.target.value))} />
        </label>
        <span className="muted">開始時に {seconds(params.bgm_fade_in_s ?? 2)} でフェードイン</span>
      </div>
      <h4>終了タイミング</h4>
      <div className="mix-timing-row">
        <label>話者終了後にBGMを残す時間 (秒)
          <input type="number" min="0" step="0.1" value={params.bgm_tail_s ?? ""}
            placeholder="従来の長さ"
            onChange={e => onChange(["bgm_tail_s"], e.target.value === "" ? null : Number(e.target.value))} />
        </label>
        <span className="muted">{params.bgm_tail_s == null
          ? "未指定: 従来の長さを維持"
          : `話者終了から ${seconds(params.bgm_tail_s)} 後に終了`}。最後の最大3秒でフェードアウト</span>
      </div>
      <h4>ミックス設定</h4>
      <ParamFields value={{
        pan_width: params.pan_width, premix_gain_db: params.premix_gain_db,
        bgm_bed_db: params.bgm_bed_db, duck_threshold_db: params.duck_threshold_db,
        duck_ratio: params.duck_ratio, bgm_loop_crossfade_s: params.bgm_loop_crossfade_s,
      }} path={[]} onChange={onChange} />
    </div>
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
  if (stage === "trim") {
    return <ReportTable rows={[
      ["指定開始", r.requested_start_s == null ? "0 (既定)" : `${r.requested_start_s} 秒`],
      ["指定終了", r.requested_end_s == null ? "Sync の全長 (既定)" : `${r.requested_end_s} 秒`],
      ["適用範囲", `${r.applied_start_s} - ${r.applied_end_s} 秒`],
      ["出力長", `${r.output_length_s} 秒`],
      ["境界", `${r.start_sample} - ${r.end_sample} samples @ ${r.sample_rate}Hz`],
    ]} />;
  }
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
      ["ジングル開始", `${r.jingle_start_s} 秒 (終了 ${r.jingle_end_s} 秒)`],
      ["話者開始", `${r.voice_start_s} 秒 (終了 ${r.voice_end_s} 秒)`],
      ["BGM開始", `${r.bgm_start_s} 秒 (${r.bgm_fade_in_s} 秒フェードイン、終了 ${r.bgm_end_s} 秒)`],
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
  const savedParams = JSON.stringify(state.params);

  // 承認されたら自動で折りたたむ(手動トグルは維持)
  useEffect(() => { setOpen(status !== "approved"); }, [status]);
  // 同じ設定を返す定期取得では、未実行の編集値を上書きしない。
  useEffect(() => { setParams(JSON.parse(savedParams)); }, [savedParams]);

  const upstreamApproved = project.stage_order
    .slice(0, project.stage_order.indexOf(stage))
    .every(s => project.stages[s].effective_status === "approved");
  const assetsReady = project.assets_ready;
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
        <h3>{stageLabel(stage, project)}</h3>
        {status === "running" && state.progress && (
          <span className="progress-msg">{state.progress}</span>
        )}
        <span className={`badge ${status}`}>{STATUS_LABELS[status]}</span>
      </div>
      {open && (
        <div onClick={e => e.stopPropagation()}>
          {stage === "trim" ? (
            <TrimView
              projectId={project.id} speakers={speakerRoles(project)} params={params}
              programLengthSamples={Number(project.stages.sync?.report?.program_length_samples ?? 0)}
              onChange={(start, end) => {
                setParam(["start_s"], start);
                setParam(["end_s"], end);
              }}
            />
          ) : stage === "mix" ? (
            <div className="params">
              <MixFields params={params}
                jingleDuration={Number(project.assets.jingle?.probe?.duration ?? 0)}
                onChange={setParam} />
            </div>
          ) : Object.keys(params).length > 0 && (
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
                  {tracksFor(stage, project).map(t => (
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
