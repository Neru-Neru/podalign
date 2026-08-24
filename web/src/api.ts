// API クライアント + 型定義(project.json のスキーマは設計 §5.2)

export type StageStatus =
  | "pending" | "running" | "done" | "failed" | "approved" | "stale";

export interface StageState {
  params: Record<string, unknown>;
  status: StageStatus;
  effective_status: StageStatus;
  artifacts_evicted: boolean;
  started_at: string | null;
  finished_at: string | null;
  progress: string;
  log_tail: string[];
  report: Record<string, any>;
}

export interface AssetState {
  path: string;
  orig_name?: string;
  bytes: number;
  received: number;
  status: "uploading" | "processing" | "ready" | "failed";
  error?: string;
}

export interface Project {
  id: string;
  name: string;
  created_at: string;
  assets: Record<string, AssetState>;
  stage_order: string[];
  stages: Record<string, StageState>;
}

export interface ProjectSummary {
  id: string;
  name: string;
  created_at: string;
  assets_ready: boolean;
}

export const ROLES = [
  "speaker_a", "speaker_b", "speaker_c", "reference", "jingle", "bgm",
] as const;

export const ROLE_LABELS: Record<string, string> = {
  speaker_a: "話者A", speaker_b: "話者B", speaker_c: "話者C",
  reference: "リファレンス(通話録音)", jingle: "ジングル", bgm: "BGM",
};

export const STAGE_LABELS: Record<string, string> = {
  ingest: "0. Ingest(取り込み・検証)",
  sync: "1. Sync(音合わせ)",
  cleanup: "2. Cleanup(整音)",
  dynamics: "3. Dynamics(コンプ・EQ)",
  mix: "4. Mix",
  master: "5. Master",
  export: "6. Export",
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  listProjects: () => req<ProjectSummary[]>("/api/projects"),
  createProject: (name: string) =>
    req<Project>("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  getProject: (id: string) => req<Project>(`/api/projects/${id}`),
  deleteProject: (id: string) =>
    req<{ ok: boolean }>(`/api/projects/${id}`, { method: "DELETE" }),
  runStage: (id: string, stage: string, params?: Record<string, unknown>) =>
    req<{ ok: boolean }>(`/api/projects/${id}/stages/${stage}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params ? { params } : {}),
    }),
  approveStage: (id: string, stage: string) =>
    req<{ ok: boolean }>(`/api/projects/${id}/stages/${stage}/approve`, {
      method: "POST",
    }),
  usage: (id: string) =>
    req<{ total_bytes: number; by_dir: Record<string, number>; disk_free_bytes: number }>(
      `/api/projects/${id}/usage`),
  gc: (id: string) =>
    req<{ freed_bytes: number }>(`/api/projects/${id}/gc`, { method: "POST" }),

  initAsset: (id: string, role: string, bytes: number, filename: string) =>
    req<{ received: number }>(`/api/projects/${id}/assets/${role}/init`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bytes, filename }),
    }),
  previewUrl: (id: string, stage: string, name: string) =>
    `/api/projects/${id}/stages/${stage}/preview?name=${name}`,
  peaksUrl: (id: string, stage: string, name: string) =>
    `/api/projects/${id}/stages/${stage}/peaks?name=${name}`,
  downloadUrl: (id: string, artifact: string) =>
    `/api/projects/${id}/download/${artifact}`,
};

const CHUNK = 8 * 1024 * 1024;

/** File.slice() 分割アップロード。409 の received からレジューム(§6.3)。 */
export async function uploadAsset(
  id: string, role: string, file: File, onProgress: (received: number) => void,
): Promise<void> {
  const { received } = await api.initAsset(id, role, file.size, file.name);
  let offset = received;
  while (offset < file.size) {
    const chunk = file.slice(offset, offset + CHUNK);
    const res = await fetch(
      `/api/projects/${id}/assets/${role}/chunk?offset=${offset}`,
      { method: "PUT", body: chunk },
    );
    if (res.status === 409) {
      const body = await res.json();
      const r = body.detail?.received;
      if (typeof r === "number") { offset = r; continue; }
      throw new Error(JSON.stringify(body.detail));
    }
    if (!res.ok) throw new Error(`アップロード失敗 (${res.status})`);
    const body = await res.json();
    offset = body.received;
    onProgress(offset);
  }
}

export function fmtBytes(b: number): string {
  if (b >= 1 << 30) return `${(b / (1 << 30)).toFixed(2)} GB`;
  if (b >= 1 << 20) return `${(b / (1 << 20)).toFixed(1)} MB`;
  return `${Math.ceil(b / 1024)} KB`;
}
