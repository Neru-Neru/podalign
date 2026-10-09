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
  document.querySelector('button').click(); await wait(50);
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
  console.log('PASS: Trim input and drag survive polling; run submits edits; saved changes refresh.');
} finally {
  await rm(directory, { recursive: true, force: true });
}
