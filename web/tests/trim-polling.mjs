import { build } from 'esbuild';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

// 実ブラウザで入力・ドラッグと、同じ保存設定を返すポーリングを再現する。
const directory = await mkdtemp(join(tmpdir(), 'podalign-trim-polling-'));
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
  document.getElementById('result').textContent = 'PASS';
})().catch(error => { document.getElementById('result').textContent = 'FAIL: ' + error.message; });
` },
    bundle: true, write: false, format: 'iife', define: { 'process.env.NODE_ENV': '"production"' },
  });
  const html = join(directory, 'test.html');
  await writeFile(html, `<div id="root"></div><pre id="result">RUNNING</pre><script>${bundle.outputFiles[0].text}</script>`);
  const result = spawnSync(process.env.CHROME_BIN || 'google-chrome', [
    '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    `--user-data-dir=${join(directory, 'profile')}`, '--dump-dom', '--virtual-time-budget=8000', `file://${html}`,
  ], { encoding: 'utf8', timeout: 30000, maxBuffer: 2 * 1024 * 1024 });
  if (!result.stdout?.includes('<pre id="result">PASS</pre>')) {
    throw Error(result.stdout?.match(/<pre id="result">(.*?)<\/pre>/)?.[1] || result.error?.message || result.stderr);
  }
  console.log('PASS: Trim preview uses Sync audio, respects selection, handles errors; edits survive polling and submit correctly.');
} finally {
  await rm(directory, { recursive: true, force: true });
}
