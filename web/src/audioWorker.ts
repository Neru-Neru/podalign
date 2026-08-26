import { ROLE_LABELS } from "./browserPipeline";

type Process = { type: "process"; role: string; channels: Float32Array[]; sampleRate: number };
let cancelled = false;
let reference: Float32Array | null = null;
let bgm: Float32Array | null = null;
let jingle: Float32Array | null = null;
let sampleRate = 48000;
const speakers: Float32Array[] = [];
const post = (message: unknown, transfer: Transferable[] = []) => self.postMessage(message, { transfer });
const progress = (text: string) => post({ type: "progress", text });
const mono = (channels: Float32Array[]) => {
  if (channels.length === 1) return channels[0];
  const n = Math.min(...channels.map(c => c.length)); const out = new Float32Array(n);
  for (let i = 0; i < n; i++) for (const c of channels) out[i] += c[i] / channels.length;
  return out;
};
const resample = (x: Float32Array, from: number, to: number) => {
  if (from === to) return x;
  const out = new Float32Array(Math.round(x.length * to / from));
  for (let i = 0; i < out.length; i++) { const p = i * from / to, a = Math.floor(p), f = p - a; out[i] = (x[a] ?? 0) * (1 - f) + (x[a + 1] ?? 0) * f; }
  return out;
};
const clean = (x: Float32Array) => {
  const out = new Float32Array(x.length); let hp = 0, prev = 0;
  for (let i = 0; i < x.length; i++) { hp = 0.995 * (hp + x[i] - prev); prev = x[i]; out[i] = Math.tanh(hp * 1.15); }
  return out;
};
const rms = (x: Float32Array) => { let s = 0; for (const v of x) s += v * v; return Math.sqrt(s / Math.max(1, x.length)); };
const wav = (left: Float32Array, right: Float32Array, sr: number) => {
  const n = Math.min(left.length, right.length), data = new ArrayBuffer(44 + n * 4), v = new DataView(data);
  const put = (o: number, s: string) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  put(0, "RIFF"); v.setUint32(4, 36 + n * 4, true); put(8, "WAVE"); put(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 3, true); v.setUint16(22, 2, true); v.setUint32(24, sr, true); v.setUint32(28, sr * 8, true); v.setUint16(32, 8, true); v.setUint16(34, 32, true); put(36, "data"); v.setUint32(40, n * 4, true);
  for (let i = 0; i < n; i++) { v.setFloat32(44 + i * 4, Math.max(-1, Math.min(1, (left[i] + right[i]) * 0.5)), true); }
  return data;
};
self.onmessage = (event: MessageEvent<Process | { type: "cancel" }>) => {
  if (event.data.type === "cancel") { cancelled = true; return; }
  const { role, channels, sampleRate: sr } = event.data; sampleRate = 48000;
  try {
    if (role === "reference") { reference = resample(mono(channels), sr, sampleRate); progress("リファレンスを準備しました"); post({ type: "ack" }); return; }
    if (role === "bgm") { bgm = resample(mono(channels), sr, sampleRate); progress("BGMを準備しました"); post({ type: "ack" }); return; }
    if (role === "jingle") { jingle = resample(mono(channels), sr, sampleRate); progress("ジングルを準備しました"); post({ type: "ack" }); return; }
    if (!reference) throw new Error("リファレンスが未準備です");
    const ref = reference;
    const input = resample(mono(channels), sr, sampleRate);
    progress(`${ROLE_LABELS[role] ?? role}: offset / drift 推定中`);
    // Envelope correlation preserves the existing offset-first design without a main-thread FFT.
    const step = 4800, limit = Math.min(input.length, ref.length), max = Math.min(24000, limit - step);
    const scoreLag = (lag: number, start: number, width: number) => { let s = 0, count = 0; for (let i = start; i < Math.min(limit - width, start + width); i += 240) { const j = i + lag; if (j >= 0 && j < input.length) { s += Math.abs(input[j]) * Math.abs(ref[i]); count++; } } return s / Math.max(1, count); };
    let best = -max, score = -Infinity;
    for (let lag = -max; lag <= max; lag += step) { const value = scoreLag(lag, 0, Math.min(limit, sampleRate * 30)); if (value > score) { score = value; best = lag; } }
    for (let lag = best - step; lag <= best + step; lag += 120) { const value = scoreLag(lag, 0, Math.min(limit, sampleRate * 60)); if (value > score) { score = value; best = lag; } }
    // Two segment refinements estimate clock drift and avoid keeping a second full copy.
    const t0 = Math.floor(limit * 0.2), t1 = Math.floor(limit * 0.8), radius = sampleRate * 2;
    const refine = (center: number, t: number) => { let found = center, top = -Infinity; for (let lag = center - radius; lag <= center + radius; lag += 120) { const value = scoreLag(lag, Math.max(0, t - sampleRate * 10), sampleRate * 20); if (value > top) { top = value; found = lag; } } return found; };
    const lag0 = refine(best, t0), lag1 = refine(best, t1), drift = (lag1 - lag0) / Math.max(1, t1 - t0);
    progress(`${ROLE_LABELS[role] ?? role}: offset ${(best / sampleRate * 1000).toFixed(1)}ms / drift ${(drift * 1e6).toFixed(1)}ppm`);
    const aligned = new Float32Array(reference.length);
    for (let i = 0; i < aligned.length; i++) { const source = Math.round(i + best + drift * (i - t0)); aligned[i] = input[source] ?? 0; }
    const processed = clean(aligned); speakers.push(processed);
    if (speakers.length < 3) { post({ type: "ack" }); return; }
    if (cancelled) throw new Error("キャンセルされました");
    progress("mix: BGM ducking + master normalization");
    const n = Math.min(reference.length, ...speakers.map(s => s.length)); const l = new Float32Array(n), r = new Float32Array(n);
    const musicTrack = bgm ?? reference;
    const gain = -16 / 20 * Math.log10(Math.max(1e-6, rms(speakers[0])));
    for (let i = 0; i < n; i++) { if (cancelled) throw new Error("キャンセルされました"); const voice = (speakers[0][i] + speakers[1][i] + speakers[2][i]) / 3; const duck = Math.max(0.15, 1 - Math.min(0.85, Math.abs(voice) * 2)); const music = (musicTrack[i % musicTrack.length] ?? 0) * duck * 0.12; const intro = jingle && i < Math.min(jingle.length, sampleRate * 3) ? jingle[i] * 0.2 : 0; l[i] = (voice * 0.9 + music + intro) * gain; r[i] = (voice * 1.1 + music + intro) * gain; if (i % Math.max(1, Math.floor(n / 100)) === 0) progress(`mix / master ${Math.floor(i / n * 100)}%`); }
    const output = wav(l, r, sampleRate); post({ type: "result", wav: output }, [output]);
  } catch (e) { post({ type: "error", message: e instanceof Error ? e.message : String(e) }); }
};
