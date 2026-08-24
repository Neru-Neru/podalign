import { StageState } from "../api";

interface Segment { t: number; lag_ms: number; confidence: number; used: boolean; }

const COLORS: Record<string, string> = {
  speaker_a: "#4f8ef7", speaker_b: "#3fb96f", speaker_c: "#e0a63c",
};

/** Sync 専用ビュー: 「時間 vs ずれ」グラフ + 推定値・残差テーブル(§9)。 */
export default function SyncView({ stage }: { stage: StageState }) {
  const report = stage.report;
  if (!report?.segments) return null;
  const speakers = Object.keys(report.segments) as string[];

  const all: Segment[] = speakers.flatMap(s => report.segments[s]);
  if (all.length === 0) return null;
  const tMax = Math.max(...all.map(s => s.t));
  const lagMin = Math.min(...all.map(s => s.lag_ms));
  const lagMax = Math.max(...all.map(s => s.lag_ms));
  const pad = Math.max(1, (lagMax - lagMin) * 0.15);
  const W = 640, H = 220, ML = 60, MB = 24;
  const x = (t: number) => ML + (t / tMax) * (W - ML - 10);
  const y = (lag: number) =>
    10 + (1 - (lag - (lagMin - pad)) / ((lagMax + pad) - (lagMin - pad))) * (H - MB - 20);

  return (
    <div>
      <p className="muted" style={{ margin: "8px 0 4px" }}>
        時間 vs ずれ(点 = 区間別推定、×= 除外区間、線 = フィット結果)
      </p>
      <svg width={W} height={H} className="svg-chart">
        {[lagMin, (lagMin + lagMax) / 2, lagMax].map((v, i) => (
          <g key={i}>
            <line x1={ML} x2={W - 10} y1={y(v)} y2={y(v)} stroke="#3a3f47" strokeDasharray="3 3" />
            <text x={4} y={y(v) + 4} fill="#9aa1ab" fontSize={10}>{v.toFixed(1)}ms</text>
          </g>
        ))}
        <text x={W / 2} y={H - 4} fill="#9aa1ab" fontSize={10}>収録時間(秒)</text>
        {speakers.map(sp => {
          const d0 = report.offsets_ms[sp];
          const slope = report.drift_ppm[sp] / 1000;  // ppm → ms/s
          return (
            <g key={sp}>
              <line
                x1={x(0)} y1={y(d0)} x2={x(tMax)} y2={y(d0 + slope * tMax)}
                stroke={COLORS[sp]} strokeWidth={1.2} opacity={0.7}
              />
              {(report.segments[sp] as Segment[]).map((seg, i) =>
                seg.used ? (
                  <circle key={i} cx={x(seg.t)} cy={y(seg.lag_ms)} r={3.2} fill={COLORS[sp]} />
                ) : (
                  <text key={i} x={x(seg.t) - 4} y={y(seg.lag_ms) + 4}
                    fill="#e05c5c" fontSize={11}>×</text>
                ),
              )}
            </g>
          );
        })}
      </svg>
      <div className="report">
        <table>
          <thead>
            <tr><th>トラック</th><th>オフセット</th><th>ドリフト</th><th>補正</th><th>補正後残差</th></tr>
          </thead>
          <tbody>
            {speakers.map(sp => (
              <tr key={sp}>
                <td style={{ color: COLORS[sp] }}>{sp}</td>
                <td>{report.offsets_ms[sp].toFixed(1)} ms</td>
                <td>{report.drift_ppm[sp].toFixed(1)} ppm</td>
                <td>{report.drift_corrected[sp] ? "rubberband" : "オフセットのみ"}</td>
                <td style={{ color: Math.abs(report.residual_ms[sp]) > 1 ? "var(--warn)" : "var(--ok)" }}>
                  {report.residual_ms[sp].toFixed(2)} ms
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
