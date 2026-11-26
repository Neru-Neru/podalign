import { build } from 'esbuild';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';

// 実ブラウザで入力・ドラッグと、同じ保存設定を返すポーリングを再現する。
const directory = await mkdtemp(join(tmpdir(), 'podalign-trim-polling-'));
let server;
try {
  const bundle = await build({
    stdin: {
      resolveDir: resolve('src'), loader: 'tsx', contents: `
import React from 'react';
import { createRoot } from 'react-dom/client';
import StagePanel from './components/StagePanel';
const stage = (params = {}) => ({ params, status: 'pending', effective_status: 'pending',
  artifacts_evicted: false, started_at: null, finished_at: null, progress: '', log_tail: [], report: {} });
const project = { id: 'test', name: 'test', assets_ready: true,
  assets: { speaker_a: { status: 'ready' }, speaker_b: { status: 'ready' } },
  stage_order: ['sync', 'trim'], stages: {
    sync: { ...stage(), effective_status: 'approved', report: { program_length_samples: 480000 } },
    trim: stage({ start_s: 0, end_s: null }),
  } };
let submitted;
let playing = false;
let rejectPlay = false;
HTMLMediaElement.prototype.play = function () {
  if (rejectPlay) return Promise.reject(Error('play failed'));
  playing = true;
  return Promise.resolve();
};
HTMLMediaElement.prototype.pause = function () { playing = false; };
window.fetch = async (url, options) => {
  if (String(url).endsWith('/run')) submitted = JSON.parse(options.body);
  return new Response(JSON.stringify({ peaks: [0.2, 0.4], duration: 10 }), { status: 200 });
};
const root = createRoot(document.getElementById('root'));
const render = () => root.render(<StagePanel project={structuredClone(project)} stage="trim" anyRunning={false} onAction={() => {}} />);
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const values = () => [...document.querySelectorAll('.trim-fields input')].map(input => Number(input.value));
const check = (expected, label) => {
  if (JSON.stringify(values()) !== JSON.stringify(expected)) throw Error(label + ': ' + JSON.stringify(values()));
};
const input = (index, value) => {
  const element = document.querySelectorAll('.trim-fields input')[index];
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(element, String(value));
  element.dispatchEvent(new Event('input', { bubbles: true }));
};
(async () => {
  render(); await wait(100);
  input(0, 1); await wait(50);
  input(1, 9); await wait(50);
  check([1, 9], 'typed values');
  const previewButton = () => [...document.querySelectorAll('button')].find(b => b.textContent === '選択範囲を試聴');
  const audio = () => document.querySelector('audio');
  const ready = async () => { audio().dispatchEvent(new Event('loadedmetadata')); await wait(20); };
  if (!audio().src.includes('/stages/sync/preview?name=reference')) throw Error('preview must use Sync reference before Trim runs');
  if (!previewButton().disabled) throw Error('preview enabled before metadata');
  await ready();
  previewButton().click(); await wait(20);
  if (!playing || audio().currentTime !== 1 || submitted) throw Error('preview must start at selection without running Trim');
  audio().currentTime = 8; audio().dispatchEvent(new Event('timeupdate'));
  if (!playing) throw Error('preview stopped inside selection');
  audio().currentTime = 9; audio().dispatchEvent(new Event('timeupdate'));
  if (playing) throw Error('preview did not stop at selection end');
  previewButton().click(); await wait(20);
  input(0, 2); await wait(20);
  if (playing) throw Error('selection edit must stop range playback');
  input(0, 1); await wait(20);
  const selector = document.querySelector('select');
  if ([...selector.options].some(option => option.value === 'speaker_c')) throw Error('absent speaker offered');
  selector.value = 'speaker_a'; selector.dispatchEvent(new Event('change', { bubbles: true })); await wait(20);
  if (!audio().src.includes('/stages/sync/preview?name=speaker_a') || !previewButton().disabled) throw Error('track switch must reload Sync preview');
  await ready();
  rejectPlay = true; previewButton().click(); await wait(20);
  if (!document.querySelector('[role="alert"]')) throw Error('play failure must be visible');
  rejectPlay = false;
  input(1, 1); await wait(20);
  if (!previewButton().disabled) throw Error('empty selection preview enabled');
  input(1, 9); await wait(20);
  audio().dispatchEvent(new Event('error')); await wait(20);
  if (!previewButton().disabled || !document.querySelector('[role="alert"]')) throw Error('load failure must disable range preview and show error');
  await ready();
  const timer = setInterval(render, 1000);
  await wait(2200); check([1, 9], 'typed values after polling');
  const canvas = document.querySelector('canvas');
  canvas.setPointerCapture = () => {};
  const rect = canvas.getBoundingClientRect();
  const pointer = (type, fraction) => canvas.dispatchEvent(new PointerEvent(type, {
    bubbles: true, pointerId: 1, clientX: rect.left + rect.width * fraction, clientY: rect.top + 40,
  }));
  pointer('pointerdown', 0.1); pointer('pointermove', 0.3); await wait(50); pointer('pointerup', 0.3);
  check([3, 9], 'dragged handle');
  await wait(2200); check([3, 9], 'dragged handle after polling');
  [...document.querySelectorAll('button')].find(b => b.textContent === '実行').click(); await wait(50);
  if (submitted.params.start_s !== 3 || submitted.params.end_s !== 9) throw Error('run submitted old values');
  project.stages.trim.params = { start_s: 2, end_s: 8 };
  render(); await wait(50); check([2, 8], 'changed saved settings');
  clearInterval(timer);
  const mixProject = { ...project, stage_order: ['mix'], stages: { mix: stage({ bgm_tail_s: 3 }) } };
  const renderMix = () => root.render(<StagePanel key="mix" project={structuredClone(mixProject)} stage="mix" anyRunning={false} onAction={() => {}} />);
  const tailInput = () => [...document.querySelectorAll('label')].find(label => label.textContent.includes('話者終了後にBGMを残す時間')).querySelector('input');
  renderMix(); await wait(50);
  if (tailInput().value !== '3') throw Error('new Mix tail value missing');
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(tailInput(), '0');
  tailInput().dispatchEvent(new Event('input', { bubbles: true })); await wait(20);
  renderMix(); await wait(20);
  if (tailInput().value !== '0') throw Error('Mix polling reset tail edit');
  [...document.querySelectorAll('button')].find(b => b.textContent === '実行').click(); await wait(20);
  if (submitted.params.bgm_tail_s !== 0) throw Error('Mix run did not submit zero tail');
  mixProject.stages.mix.params = {};
  renderMix(); await wait(50);
  if (tailInput().value !== '' || !document.body.textContent.includes('従来の長さを維持')) throw Error('legacy Mix must keep its ending until edited');
  const cleanupProject = { ...project, stage_order: ['cleanup'], stages: {
    cleanup: stage({ highpass_hz: 80, nr_max_db: 12, gate_range_db: -12, target_lufs: -20, deess_intensity: 0.3 })
  } };
  const renderCleanup = () => root.render(<StagePanel key="cleanup" project={structuredClone(cleanupProject)} stage="cleanup" anyRunning={false} onAction={() => {}} />);
  const capInput = role => [...document.querySelectorAll('label')].find(label => label.textContent === role + ' の最大増幅量 (dB)').querySelector('input');
  renderCleanup(); await wait(50);
  if (capInput('speaker_a').value !== '' || capInput('speaker_a').placeholder !== '共通設定: 12 dB') throw Error('legacy Cleanup fallback missing');
  if (document.body.textContent.includes('speaker_c の最大増幅量')) throw Error('absent speaker cap shown');
  const setCap = async value => {
    const element = capInput('speaker_a');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(element, value);
    element.dispatchEvent(new Event('input', { bubbles: true })); await wait(20);
  };
  await setCap('0');
  renderCleanup(); await wait(50);
  if (capInput('speaker_a').value !== '0') throw Error('polling reset zero cap');
  [...document.querySelectorAll('button')].find(b => b.textContent === '実行').click(); await wait(20);
  if (submitted.params.track_max_gain_db.speaker_a !== 0 || 'speaker_b' in submitted.params.track_max_gain_db) throw Error('individual cap leaked or zero lost');
  await setCap('');
  [...document.querySelectorAll('button')].find(b => b.textContent === '実行').click(); await wait(20);
  if ('speaker_a' in submitted.params.track_max_gain_db) throw Error('clearing override must restore inheritance');
  if ('track_max_gain_db' in cleanupProject.stages.cleanup.params) throw Error('editing mutated saved settings');
  cleanupProject.stages.cleanup = { ...cleanupProject.stages.cleanup, effective_status: 'done', report: {
    tracks: { speaker_a: { noise_floor_db: -60, nr_db: 12, align_gain_db: 12,
      loudness_after_gain: -42, true_peak_after_gain: -20, gain_limit_reasons: ['max_gain'] } }, warnings: []
  } };
  renderCleanup(); await wait(50);
  if (!document.body.textContent.includes('安全制限') || !document.body.textContent.includes('-42 LUFS')) throw Error('limited output report missing');
  const nrSelect = [...document.querySelectorAll('label')].find(label => label.textContent.includes('ノイズ除去モード')).querySelector('select');
  nrSelect.value = 'auto'; nrSelect.dispatchEvent(new Event('change', { bubbles: true })); await wait(20);
  const trackNr = [...document.querySelectorAll('label')].find(label => label.textContent.startsWith('speaker_b のノイズ除去')).querySelector('select');
  trackNr.value = 'off'; trackNr.dispatchEvent(new Event('change', { bubbles: true })); await wait(20);
  [...document.querySelectorAll('button')].find(b => b.textContent === '再実行').click(); await wait(20);
  if (submitted.params.denoise_mode !== 'auto' || submitted.params.track_denoise_mode.speaker_b !== 'off') throw Error('NR selection lost');
  const syncProject = { ...project, stage_order: ['sync'], stages: { sync: {
    ...stage({ drift_threshold_ppm: 5, n_segments: 10, segment_s: 30 }), effective_status: 'done',
    report: { approval_blocked: true, segments: { speaker_a: [{ t: 10, lag_ms: 1, confidence: 20, used: true }] },
      offsets_ms: { speaker_a: 1 }, drift_ppm: { speaker_a: 0 }, drift_corrected: { speaker_a: false },
      residual_ms: { speaker_a: null } }
  } } };
  const renderSync = () => root.render(<StagePanel key="sync" project={structuredClone(syncProject)} stage="sync" anyRunning={false} onAction={() => {}} />);
  renderSync(); await wait(50);
  const approval = [...document.querySelectorAll('button')].find(b => b.textContent === '承認して次へ');
  if (!approval.disabled || !document.querySelector('[role="alert"]')) throw Error('unsafe sync approval not blocked');
  const resampler = [...document.querySelectorAll('label')].find(label => label.textContent.includes('ドリフト補正方式')).querySelector('select');
  resampler.value = 'polyphase'; resampler.dispatchEvent(new Event('change', { bubbles: true })); await wait(20);
  const offset = [...document.querySelectorAll('label')].find(label => label.textContent === 'speaker_a の手動オフセット (ms)').querySelector('input');
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(offset, '0');
  offset.dispatchEvent(new Event('input', { bubbles: true })); await wait(20);
  renderSync(); await wait(20);
  [...document.querySelectorAll('button')].find(b => b.textContent === '再実行').click(); await wait(20);
  if (submitted.params.resample_method !== 'polyphase' || submitted.params.manual_offsets_ms.speaker_a !== 0) throw Error('sync choices or zero offset lost');
  if (submitted.params.allow_low_confidence) throw Error('unsafe override enabled implicitly');
  document.getElementById('result').textContent = 'PASS';
})().catch(error => { document.getElementById('result').textContent = 'FAIL: ' + error.message; });
` },
    bundle: true, write: false, format: 'iife', define: { 'process.env.NODE_ENV': '"production"' },
  });
  const html = join(directory, 'test.html');
  await writeFile(html, `<div id="root"></div><pre id="result">RUNNING</pre><script>${bundle.outputFiles[0].text}</script>`);
  // Serve our fixture over loopback HTTP. Managed browsers may reject file://;
  // a separate process keeps serving while spawnSync waits for the browser.
  server = spawn(process.execPath, ['--input-type=module', '-e', `
    import { createServer } from 'node:http';
    import { readFile } from 'node:fs/promises';
    const html = await readFile(process.argv[1]);
    const server = createServer((req, res) => {
      if (req.url !== '/test.html') { res.writeHead(404).end(); return; }
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }).end(html);
    });
    server.listen(0, '127.0.0.1', () => console.log('http://127.0.0.1:' + server.address().port + '/test.html'));
  `, html], { stdio: ['ignore', 'pipe', 'inherit'] });
  const url = await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.once('exit', code => reject(Error('fixture server exited: ' + code)));
    server.stdout.once('data', data => resolve(String(data).trim()));
  });
  const result = spawnSync(process.env.CHROME_BIN || 'google-chrome', [
    '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    `--user-data-dir=${join(directory, 'profile')}`, '--dump-dom', '--virtual-time-budget=8000', url,
  ], { encoding: 'utf8', timeout: 30000, maxBuffer: 2 * 1024 * 1024 });
  if (!result.stdout?.includes('<pre id="result">PASS</pre>')) {
    throw Error(result.stdout?.match(/<pre id="result">(.*?)<\/pre>/)?.[1] || result.error?.message || result.stderr);
  }
  console.log('PASS: Trim preview and polling; Mix tail edit submits zero and preserves legacy settings; Cleanup caps inherit, preserve zero across polling, and show safety results; NR selection and blocked Sync approval with zero manual offsets.');
} finally {
  server?.kill();
  await rm(directory, { recursive: true, force: true });
}
