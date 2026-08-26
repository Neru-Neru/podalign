export const ROLES = ["speaker_a", "speaker_b", "speaker_c", "reference", "jingle", "bgm"] as const;
export const ROLE_LABELS: Record<string, string> = { speaker_a: "話者A", speaker_b: "話者B", speaker_c: "話者C", reference: "リファレンス", jingle: "ジングル", bgm: "BGM" };
type Role = typeof ROLES[number];
type Reply = { type: "progress"; text: string } | { type: "ack" } | { type: "result"; wav: ArrayBuffer } | { type: "error"; message: string };

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
  private async send(role: Role, audio: AudioBuffer): Promise<Reply> {
    const channels = Array.from({ length: audio.numberOfChannels }, (_, i) => audio.getChannelData(i).slice());
    const reply = this.wait();
    this.worker.postMessage({ type: "process", role, channels, sampleRate: audio.sampleRate }, channels.map(c => c.buffer));
    return reply;
  }
  async run(files: Record<Role, File>): Promise<Blob> {
    const AudioCtx = window.AudioContext || (window as typeof window & { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) throw new Error("このブラウザは Web Audio API に対応していません");
    const ctx = new AudioCtx();
    const decode = async (role: Role) => { this.onProgress(`${ROLE_LABELS[role]} をデコード中`); return ctx.decodeAudioData(await files[role].arrayBuffer()); };
    try {
      await this.send("reference", await decode("reference"));
      await this.send("jingle", await decode("jingle"));
      await this.send("bgm", await decode("bgm"));
      await this.send("speaker_a", await decode("speaker_a"));
      await this.send("speaker_b", await decode("speaker_b"));
      const final = await this.send("speaker_c", await decode("speaker_c"));
      if (final.type !== "result") throw new Error("Worker が出力を返しませんでした");
      return new Blob([final.wav], { type: "audio/wav" });
    } finally { await ctx.close(); }
  }
}
