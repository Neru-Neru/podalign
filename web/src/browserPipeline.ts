export const ROLES = ["speaker_a", "speaker_b", "speaker_c", "reference", "jingle", "bgm"] as const;
export const ROLE_LABELS: Record<string, string> = { speaker_a: "話者A", speaker_b: "話者B", speaker_c: "話者C", reference: "リファレンス", jingle: "ジングル", bgm: "BGM" };
type Role = typeof ROLES[number];
type Reply = { type: "progress"; text: string } | { type: "ack" } | { type: "result"; wav: ArrayBuffer } | { type: "error"; message: string };

/** Sends File handles to the worker; PCM is sliced there and never decoded wholesale. */
export class BrowserPipeline {
  readonly worker = new Worker(new URL("./audioWorker.ts", import.meta.url), { type: "module" });
  private resolve: ((value: Reply) => void) | null = null;
  private reject: ((e: Error) => void) | null = null;
  constructor(private readonly onProgress: (text: string) => void) {
    this.worker.onmessage = (event: MessageEvent<Reply>) => {
      const m = event.data;
      if (m.type === "progress") return this.onProgress(m.text);
      if (m.type === "error") { this.reject?.(new Error(m.message)); this.resolve = null; this.reject = null; return; }
      this.resolve?.(m); this.resolve = null; this.reject = null;
    };
  }
  private wait(): Promise<Reply> { return new Promise((resolve, reject) => { this.resolve = resolve; this.reject = reject; }); }
  private async send(role: Role, file: File): Promise<Reply> {
    const reply = this.wait();
    this.worker.postMessage({ type: "process", role, file });
    return reply;
  }
  async run(files: Record<Role, File>): Promise<Blob> {
    for (const role of ROLES) if (!files[role]) throw new Error(`${ROLE_LABELS[role]}が未選択です`);
    await this.send("reference", files.reference);
    await this.send("jingle", files.jingle);
    await this.send("bgm", files.bgm);
    await this.send("speaker_a", files.speaker_a);
    await this.send("speaker_b", files.speaker_b);
    const final = await this.send("speaker_c", files.speaker_c);
    if (final.type !== "result") throw new Error("Worker が出力を返しませんでした");
    return new Blob([final.wav], { type: "audio/wav" });
  }
}
