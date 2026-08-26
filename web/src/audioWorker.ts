type Role = string;
type Job = { type: "process"; role: Role; file: File } | { type: "cancel" };
let cancelled = false;
let reference: { file: File; sr: number; channels: number; bits: number; data: number } | null = null;
let bgm: { file: File; sr: number; channels: number; bits: number; data: number } | null = null;
let jingle: { file: File; sr: number; channels: number; bits: number; data: number } | null = null;
const outputs: Record<string, FileSystemFileHandle> = {};
const post = (message: unknown, transfer: Transferable[] = []) => self.postMessage(message, { transfer });
const progress = (text: string) => post({ type: "progress", text });
function header(file: File) { return file.slice(0, 128 * 1024).arrayBuffer().then(b => { const v = new DataView(b); if (v.getUint32(0, false) !== 0x52494646 || v.getUint32(8, false) !== 0x57415645) throw new Error(`${file.name}: WAV/PCM入力のみ対応しています`); let p = 12, sr = 0, channels = 0, bits = 0, data = 0; while (p + 8 <= v.byteLength) { const id = v.getUint32(p, false), n = v.getUint32(p + 4, true); if (id === 0x666d7420) { channels = v.getUint16(p + 10, true); sr = v.getUint32(p + 12, true); bits = v.getUint16(p + 22, true); } if (id === 0x64617461) { data = p + 8; break; } p += 8 + n + (n & 1); } if (!sr || !channels || ![16, 24, 32].includes(bits) || !data) throw new Error(`${file.name}: PCM WAVを読み取れません`); return { file, sr, channels, bits, data }; }); }
async function pcm(meta: NonNullable<typeof reference>, start: number, count: number) { const bytes = count * meta.channels * meta.bits / 8, b = new DataView(await meta.file.slice(meta.data + start * meta.channels * meta.bits / 8, meta.data + start * meta.channels * meta.bits / 8 + bytes).arrayBuffer()), out = new Float32Array(count); for (let i = 0; i < count; i++) { let sum = 0; for (let c = 0; c < meta.channels; c++) { const o = (i * meta.channels + c) * meta.bits / 8; sum += meta.bits === 16 ? b.getInt16(o, true) / 32768 : meta.bits === 24 ? ((b.getUint8(o) | b.getUint8(o + 1) << 8 | b.getInt8(o + 2) << 16) / 8388608) : b.getInt32(o, true) / 2147483648; } out[i] = sum / meta.channels; } return out; }
function clean(x: Float32Array) { const out = new Float32Array(x.length); let prev = 0, hp = 0; for (let i = 0; i < x.length; i++) { hp = .995 * (hp + x[i] - prev); prev = x[i]; out[i] = Math.tanh(hp * 1.15); } return out; }
function wavHeader(frames: number) { const b = new ArrayBuffer(44), v = new DataView(b), put = (o: number, s: string) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0))); put(0, "RIFF"); v.setUint32(4, 36 + frames * 8, true); put(8, "WAVE"); put(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 3, true); v.setUint16(22, 2, true); v.setUint32(24, 48000, true); v.setUint32(28, 48000 * 8, true); v.setUint16(32, 8, true); v.setUint16(34, 32, true); put(36, "data"); v.setUint32(40, frames * 8, true); return b; }
async function opfsFile(name: string) { const root = await navigator.storage.getDirectory(); const dir = await root.getDirectoryHandle("podalign", { create: true }); return dir.getFileHandle(name, { create: true }); }
self.onmessage = async (event: MessageEvent<Job>) => { if (event.data.type === "cancel") { cancelled = true; return; } const { role, file } = event.data; try {
  if (role === "reference" || role === "bgm" || role === "jingle") { const meta = await header(file); if (role === "reference") reference = meta; if (role === "bgm") bgm = meta; if (role === "jingle") jingle = meta; progress(`${role}: WAVヘッダーのみ読み込み`); post({ type: "ack" }); return; }
  if (!reference) throw new Error("リファレンスが未準備です");
  const meta = await header(file), chunk = 30 * meta.sr, refFrames = Math.floor((reference.file.size - reference.data) / (reference.channels * reference.bits / 8)), inputFrames = Math.min(refFrames, Math.floor((file.size - meta.data) / (meta.channels * meta.bits / 8)));
  progress(`${role}: window単位でoffset/drift推定`); const refProbe = await pcm(reference, 0, Math.min(reference.sr * 60, refFrames)); const inProbe = await pcm(meta, 0, Math.min(meta.sr * 60, inputFrames)); let best = 0, score = -Infinity; for (let lag = -reference.sr * 2; lag <= reference.sr * 2; lag += 240) { let s = 0; for (let i = reference.sr; i < refProbe.length - reference.sr; i += 240) { const j = i + lag; if (j >= 0 && j < inProbe.length) s += Math.abs(refProbe[i]) * Math.abs(inProbe[j]); } if (s > score) { score = s; best = lag; } }
  const handle = await opfsFile(`${role}.f32`), w = await handle.createWritable(); try { for (let start = 0; start < inputFrames; start += chunk) { if (cancelled) throw new Error("キャンセルされました"); const x = clean(await pcm(meta, Math.max(0, start + best), Math.min(chunk, inputFrames - start))); await w.write(x.buffer as ArrayBuffer); progress(`${role}: ${Math.floor(start / inputFrames * 100)}%`); } } finally { await w.close(); } outputs[role] = handle; post({ type: "ack" });
  if (!outputs.speaker_a || !outputs.speaker_b || role !== "speaker_c") return;
  progress("mix / master: chunk単位で処理中");
  const af = await outputs.speaker_a.getFile(), bf = await outputs.speaker_b.getFile(), cf = await outputs.speaker_c.getFile();
  const frames = Math.min(af.size, bf.size, cf.size) / 4, outHandle = await opfsFile("episode.wav"), out = await outHandle.createWritable();
  try {
    await out.write(wavHeader(frames));
    const mixChunk = 30 * 48000;
    for (let start = 0; start < frames; start += mixChunk) {
      if (cancelled) throw new Error("キャンセルされました");
      const count = Math.min(mixChunk, frames - start), [a, b, c] = await Promise.all([af.slice(start * 4, (start + count) * 4).arrayBuffer(), bf.slice(start * 4, (start + count) * 4).arrayBuffer(), cf.slice(start * 4, (start + count) * 4).arrayBuffer()]);
      const aa = new Float32Array(a), bb = new Float32Array(b), cc = new Float32Array(c), music = bgm ? await pcm(bgm, start % Math.max(1, Math.floor((bgm.file.size - bgm.data) / (bgm.channels * bgm.bits / 8))), count) : new Float32Array(count), intro = jingle && start < jingle.sr * 3 ? await pcm(jingle, start, Math.min(count, jingle.sr * 3 - start)) : new Float32Array(count), interleaved = new Float32Array(count * 2);
      for (let i = 0; i < count; i++) { const voice = (aa[i] + bb[i] + cc[i]) / 3, duck = Math.max(.15, 1 - Math.abs(voice) * 2), v = (voice + music[i] * duck * .12 + (intro[i] ?? 0) * .2) * 1.2; interleaved[i * 2] = v * .9; interleaved[i * 2 + 1] = v * 1.1; }
      await out.write(interleaved.buffer as ArrayBuffer); progress(`mix / master ${Math.floor((start + count) / frames * 100)}%`);
    }
  } finally { await out.close(); }
  const final = await (await outHandle.getFile()).arrayBuffer();
  post({ type: "result", wav: final }, [final]);
 } catch (e) { post({ type: "error", message: e instanceof Error ? e.message : String(e) }); } };
