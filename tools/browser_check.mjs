#!/usr/bin/env node
// Headless browser check for Meridian (CONTRACT.md section 12). Node 22+ (global WebSocket and fetch).
//
//   node tools/browser_check.mjs --url http://127.0.0.1:8790/ [--wait 12000] [--shot out.png]
//        [--eval "JS expression (may return a Promise)"] [--width 1600] [--height 1000] [--browser edge|chrome]
//        [--ready]   wait for document.body.dataset.ready before --wait starts (default on)
//
// Prints one JSON object: console errors/warnings, uncaught exceptions, failed requests, HTTP >= 400
// responses, the --eval result and the screenshot path. Exit code 0 unless the page threw on load.

import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
  if (a.startsWith('--')) {
    const next = all[i + 1];
    acc.push([a.slice(2), next && !next.startsWith('--') ? next : true]);
  }
  return acc;
}, []));

const url = args.url || 'http://127.0.0.1:8790/';
const wait = +(args.wait || 8000);
const width = +(args.width || 1600);
const height = +(args.height || 1000);
const browsers = {
  edge: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  chrome: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
};
const exe = browsers[args.browser || 'edge'] || browsers.edge;
const profile = mkdtempSync(join(tmpdir(), 'meridian-cdp-'));

const chromeArgs = [
  '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run',
  '--no-default-browser-check', '--disable-extensions', '--disable-background-networking', '--disable-sync',
  `--window-size=${width},${height}`, '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist',
  '--enable-webgl', '--hide-scrollbars', '--mute-audio', 'about:blank',
];

const proc = spawn(exe, chromeArgs, { stdio: ['ignore', 'ignore', 'pipe'] });
let stderr = '';
proc.stderr.on('data', d => { stderr += d.toString(); });

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function devtoolsPort() {
  const file = join(profile, 'DevToolsActivePort');
  for (let i = 0; i < 100; i++) {
    if (existsSync(file)) {
      try {
        const [port] = readFileSync(file, 'utf8').split('\n');
        if (port) return +port;
      } catch { /* Edge may still hold the file (EBUSY); try again */ }
    }
    await sleep(100);
  }
  throw new Error('browser did not start: ' + stderr.slice(0, 500));
}

const out = { url, console: [], exceptions: [], failedRequests: [], badResponses: [], evalResult: null, screenshot: null, ready: null };

try {
  const port = await devtoolsPort();
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = targets.find(t => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve: r, reject: j } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) j(new Error(msg.error.message)); else r(msg.result);
    } else if (msg.method) {
      for (const l of listeners) l(msg);
    }
  };
  const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, { resolve: res, reject: rej }); ws.send(JSON.stringify({ id: i, method, params })); });
  const requests = new Map();
  listeners.push((m) => {
    if (m.method === 'Runtime.consoleAPICalled' && ['error', 'warning', 'assert'].includes(m.params.type)) {
      out.console.push({ type: m.params.type, text: m.params.args.map(a => a.value ?? a.description ?? '').join(' ').slice(0, 600) });
    } else if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails;
      out.exceptions.push(((d.exception && d.exception.description) || d.text || '').slice(0, 800));
    } else if (m.method === 'Log.entryAdded' && ['error', 'warning'].includes(m.params.entry.level)) {
      out.console.push({ type: `log-${m.params.entry.level}`, text: `${m.params.entry.text} ${m.params.entry.url || ''}`.slice(0, 600) });
    } else if (m.method === 'Network.requestWillBeSent') {
      requests.set(m.params.requestId, m.params.request.url);
    } else if (m.method === 'Network.loadingFailed' && !m.params.canceled) {
      out.failedRequests.push({ url: (requests.get(m.params.requestId) || '').slice(0, 300), error: m.params.errorText });
    } else if (m.method === 'Network.responseReceived' && m.params.response.status >= 400) {
      out.badResponses.push({ url: m.params.response.url.slice(0, 300), status: m.params.response.status });
    }
  });
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Network.enable');
  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
  const loaded = new Promise(r => listeners.push(m => { if (m.method === 'Page.loadEventFired') r(); }));
  await send('Page.navigate', { url });
  await Promise.race([loaded, sleep(30000)]);
  if (args.ready !== 'false') {
    for (let i = 0; i < 300; i++) {
      const r = await send('Runtime.evaluate', { expression: 'document.body && document.body.dataset.ready', returnByValue: true });
      if (r.result.value) { out.ready = r.result.value; break; }
      await sleep(100);
    }
  }
  await sleep(wait);
  if (args.eval) {
    const r = await send('Runtime.evaluate', { expression: `(async () => { return (${args.eval}); })()`, awaitPromise: true, returnByValue: true, timeout: 60000 });
    out.evalResult = r.exceptionDetails ? { error: (r.exceptionDetails.exception && r.exceptionDetails.exception.description) || r.exceptionDetails.text } : r.result.value;
    if (args['wait-after']) await sleep(+args['wait-after']);
  }
  if (args.shot) {
    const shot = await send('Page.captureScreenshot', { format: 'png' });
    const p = resolve(String(args.shot));
    writeFileSync(p, Buffer.from(shot.data, 'base64'));
    out.screenshot = p;
  }
  ws.close();
} catch (err) {
  out.fatal = String(err && err.stack || err);
} finally {
  proc.kill();
  await sleep(300);
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* locked files */ }
}
console.log(JSON.stringify(out, null, 2));
process.exit(out.fatal ? 1 : 0);
