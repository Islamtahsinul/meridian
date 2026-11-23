// Tests for the assistant's browser code that can run without a browser: the safe Markdown
// renderer (web/js/layers/ai/markdown.js) and the map-action checks (web/js/layers/ai/actions.js).
//
//   node tests/test_ai_markdown.mjs        (also run by python -m unittest tests.test_ai)

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseInline, parseMarkdown, renderMarkdown } from '../web/js/layers/ai/markdown.js';
import { validateAction, validateActions, describeActions, cleanEntity, runActions } from '../web/js/layers/ai/actions.js';
import { safeUrl, fmt } from '../web/js/core/dom.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

// A stand-in for ctx.h: records the tree instead of building DOM. Like the real h(), strings become
// text nodes and child arrays are flattened.
function h(tag, attrs, ...children) {
  const node = { tag, attrs: {}, children: [] };
  for (const [k, v] of Object.entries(attrs || {})) if (v != null && v !== false) node.attrs[k] = String(v);
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false || c === '') continue;
    node.children.push(typeof c === 'object' ? c : { text: String(c) });
  }
  return node;
}

const render = (md) => renderMarkdown(md, { h, safeUrl });
const ALLOWED_TAGS = new Set(['p', 'strong', 'em', 'code', 'pre', 'a', 'ul', 'ol', 'li', 'br', 'blockquote', 'hr', 'div', 'table', 'thead', 'tbody', 'tr', 'th', 'td']);
const ALLOWED_ATTRS = new Set(['href', 'title', 'class', 'start']);

function walk(nodes, fn) {
  for (const n of [nodes].flat()) {
    fn(n);
    if (n.children) walk(n.children, fn);
  }
}
function text(nodes) {
  let s = '';
  walk(nodes, n => { if (n.text !== undefined) s += n.text; });
  return s;
}
function tags(nodes) {
  const out = [];
  walk(nodes, n => { if (n.tag) out.push(n.tag); });
  return out;
}
function checkSafe(nodes) {
  walk(nodes, (n) => {
    if (n.tag === undefined) { assert.equal(typeof n.text, 'string'); return; }
    assert.ok(ALLOWED_TAGS.has(n.tag), `unexpected tag ${n.tag}`);
    for (const [k, v] of Object.entries(n.attrs)) {
      assert.ok(ALLOWED_ATTRS.has(k), `unexpected attribute ${k}`);
      if (k === 'href') assert.match(v, /^https?:\/\/[^/\s]/i);
    }
  });
}

let passed = 0;
const asyncTests = [];
function test(name, fn) {
  let result;
  try { result = fn(); } catch (err) { console.error(`FAIL ${name}`); throw err; }
  if (result && typeof result.then === 'function') {
    asyncTests.push(result.then(() => { passed++; }, (err) => { console.error(`FAIL ${name}`); throw err; }));
  } else passed++;
}

test('HTML stays text', () => {
  for (const evil of ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '<a href="javascript:alert(1)">x</a>',
    '"><svg onload=alert(1)>', '<iframe src="https://evil.example"></iframe>', '&lt;b&gt; &amp;']) {
    const out = render(evil);
    checkSafe(out);
    assert.equal(out[0].tag, 'p');
    assert.ok(tags(out).every(t => t === 'p' || t === 'a'), tags(out).join());   // a bare https URL may become a link
    assert.equal(text(out), evil);
  }
});

test('emphasis, code and line breaks', () => {
  const out = render('**Bold** and *italic* and __also bold__ and _it_ and `x < y && z`\nnext line');
  checkSafe(out);
  const p = out[0];
  assert.equal(p.tag, 'p');
  assert.deepEqual(p.children.map(c => c.tag || 'text'), ['strong', 'text', 'em', 'text', 'strong', 'text', 'em', 'text', 'code', 'br', 'text']);
  assert.equal(text(p.children[8]), 'x < y && z');
});

test('snake_case and lone markers stay literal', () => {
  const out = render('Use fly_to and set_layers, 2 * 3 * 4, a*b, __init__ ok');
  checkSafe(out);
  assert.ok(!tags(out).includes('em'));
  assert.ok(text(out).includes('fly_to and set_layers'));
  assert.ok(text(out).includes('2 * 3 * 4'));
});

test('links: http(s) only', () => {
  let out = render('[OFAC list](https://ofac.treasury.gov/sdn_list) and https://example.org/a_(b). Done');
  checkSafe(out);
  const links = [];
  walk(out, n => { if (n.tag === 'a') links.push(n.attrs.href); });
  assert.deepEqual(links, ['https://ofac.treasury.gov/sdn_list', 'https://example.org/a_(b)']);
  assert.ok(text(out).endsWith('. Done'));
  for (const bad of ['[x](javascript:alert(1))', '[x](JAVASCRIPT:alert(1))', '[x](data:text/html;base64,PHNjcmlwdD4=)',
    '[x](/api/core/status)', '[x](//evil.example/a)', '[x](vbscript:msgbox)', '[x](file:///C:/Windows)', '[x]( java\nscript:alert(1))']) {
    out = render(bad);
    checkSafe(out);
    assert.ok(!tags(out).includes('a'), bad);
  }
  out = render('[**bold** link](https://example.org "Title")');
  checkSafe(out);
  const a = out[0].children[0];
  assert.equal(a.tag, 'a');
  assert.equal(a.attrs.href, 'https://example.org');
  assert.equal(a.attrs.title, 'https://example.org');
  assert.equal(a.children[0].tag, 'strong');
});

test('lists, nesting and numbering', () => {
  const out = render('Ships near the strait:\n\n- **Tanker A**, 12 km\n- Tanker B\n  - anchored\n  - AIS 2 h old\n- Tanker C\n\n3. third\n4. fourth');
  checkSafe(out);
  assert.deepEqual(out.map(n => n.tag), ['p', 'ul', 'ol']);
  const ul = out[1];
  assert.equal(ul.children.length, 3);
  assert.equal(ul.children[1].children.at(-1).tag, 'ul');
  assert.equal(ul.children[1].children.at(-1).children.length, 2);
  assert.equal(out[2].attrs.start, '3');
  const blocks = parseMarkdown('1. one\n2. two');
  assert.equal(blocks[0].type, 'ol');
  assert.equal(blocks[0].items.length, 2);
});

test('code fences, quotes, rules, headings and tables', () => {
  const out = render('### Summary\n\n```\n<b>raw</b> **not bold**\n```\n\n> quoted *text*\n\n---\n\n| Ship | Speed |\n|---|---|\n| A | 12 kn |');
  checkSafe(out);
  assert.deepEqual(out.map(n => n.tag), ['p', 'pre', 'blockquote', 'hr', 'div']);
  assert.equal(out[0].attrs.class, 'ai-md-h');
  assert.equal(text(out[1]), '<b>raw</b> **not bold**');
  assert.ok(tags(out[2]).includes('em'));
  assert.deepEqual(tags(out[4]), ['div', 'table', 'thead', 'tr', 'th', 'th', 'tbody', 'tr', 'td', 'td']);
});

test('escapes and inline parsing', () => {
  assert.deepEqual(parseInline('\\*not em\\*'), [{ type: 'text', text: '*not em*' }]);
  assert.deepEqual(parseInline('`a` b').map(n => n.type), ['code', 'text']);
  assert.deepEqual(parseInline('unclosed **bold'), [{ type: 'text', text: 'unclosed **bold' }]);
});

test('hostile input stays fast and bounded', () => {
  const started = Date.now();
  for (const s of ['*'.repeat(20000), '['.repeat(5000) + ']('.repeat(5000), '_a'.repeat(10000), '- '.repeat(8000),
    '> '.repeat(4000) + 'x', '`'.repeat(9999), 'https://'.repeat(3000), '**a'.repeat(6000)]) {
    checkSafe(render(s));
  }
  assert.ok(Date.now() - started < 4000, `took ${Date.now() - started} ms`);
  assert.ok(text(render('x'.repeat(50000))).length <= 20000);
});

test('no HTML parsing APIs in the assistant browser code', () => {
  for (const file of ['web/js/layers/ai.js', 'web/js/layers/ai/markdown.js', 'web/js/layers/ai/actions.js']) {
    const src = readFileSync(join(ROOT, file), 'utf8');
    for (const bad of ['innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function', 'DOMParser']) {
      assert.ok(!src.includes(bad), `${file} uses ${bad}`);
    }
  }
});

// ------------------------------------------------------------------ map actions
const layersOn = new Set(['flights']);
const ctx = {
  ai: { actions: () => ['fly_to', 'fit_bounds', 'set_layers', 'set_basemap', 'set_projection', 'set_terrain', 'open_entity', 'whats_here', 'highlight'] },
  layers: { get: id => (['flights', 'ships', 'cameras'].includes(id) ? { spec: { id, title: { flights: 'Aircraft', ships: 'Ships', cameras: 'Cameras' }[id], group: 'X' } } : null),
    list: () => [...layersOn].map(id => ({ id, on: true })) },
  map2: { BASEMAPS: [{ id: 'dark', title: 'Dark' }, { id: 'satellite', title: 'Satellite' }] },
  fmt,
};

test('actions: known types and ranges only', () => {
  assert.equal(validateAction({ type: 'eval', code: 'x' }, ctx).ok, false);
  assert.equal(validateAction({ type: 'fly_to', lat: 91, lon: 0 }, ctx).ok, false);
  assert.equal(validateAction({ type: 'fly_to', lat: '10', lon: 0 }, ctx).ok, false);
  assert.equal(validateAction({ type: 'fly_to', lat: 10, lon: 0, zoom: 99 }, ctx).ok, false);
  assert.deepEqual(validateAction({ type: 'fly_to', lat: 26.5, lon: 56.2, zoom: 0.5, extra: '<script>' }, ctx),
    { ok: true, action: { type: 'fly_to', lat: 26.5, lon: 56.2, zoom: 1 } });
  assert.deepEqual(validateAction({ type: 'fit_bounds', west: 170, south: -20, east: -170, north: -10 }, ctx).action,
    { type: 'fit_bounds', west: 170, south: -20, east: 190, north: -10 });
  assert.equal(validateAction({ type: 'fit_bounds', west: 0, south: 10, east: 1, north: 5 }, ctx).ok, false);
  assert.deepEqual(validateAction({ type: 'set_layers', on: ['ships', 'nope', 5], off: ['cameras', 'ships'] }, ctx).action,
    { type: 'set_layers', on: ['ships'], off: ['cameras'] });
  assert.equal(validateAction({ type: 'set_layers', on: ['nope'] }, ctx).ok, false);
  assert.equal(validateAction({ type: 'set_basemap', id: 'neon' }, ctx).ok, false);
  assert.equal(validateAction({ type: 'set_projection', projection: 'globe' }, ctx).ok, true);
  assert.equal(validateAction({ type: 'set_terrain', on: 'yes' }, ctx).ok, false);
  const ent = validateAction({ type: 'open_entity', kind: 'ship', id: '235000001', title: 'X', lat: 50, lon: -1, layer: 'nope', onclick: 'x' }, ctx);
  assert.deepEqual(ent.action, { type: 'open_entity', kind: 'ship', id: '235000001', title: 'X', lat: 50, lon: -1 });
  assert.equal(cleanEntity({ kind: 'Bad Kind', id: '1' }, ctx), null);
});

test('actions: run order and plain description', () => {
  const { valid, dropped } = validateActions([
    { type: 'set_layers', on: ['ships'], off: [] }, { type: 'fly_to', lat: 26.5667, lon: 56.25, zoom: 7 },
    { type: 'set_basemap', id: 'satellite' }, { type: 'nope' }], ctx);
  assert.deepEqual(valid.map(a => a.type), ['set_basemap', 'set_layers', 'fly_to']);
  assert.equal(dropped.length, 1);
  assert.equal(describeActions(valid, ctx), 'Moved the map to 26.57° N, 56.25° E. Switched the basemap to Satellite. Turned on Ships.');
  assert.equal(describeActions([{ type: 'set_layers', on: ['ships', 'flights', 'cameras'], off: [] }], ctx), 'Turned on Ships, Aircraft and Cameras.');
});

test('actions: a card and a camera move to the same place become one move', async () => {
  const ran = [];
  const runCtx = { ...ctx, ai: { ...ctx.ai, run: async (a) => { ran.push({ ...a }); } }, map2: { ...ctx.map2, setProjection: (p) => ran.push({ type: 'proj', p }) } };
  const { valid } = validateActions([{ type: 'fly_to', lat: 51.47, lon: -0.4543, zoom: 12.5 },
    { type: 'open_entity', kind: 'airport', id: 'EGLL', title: 'London Heathrow Airport', lat: 51.47, lon: -0.4543 },
    { type: 'set_projection', projection: 'globe' }], runCtx);
  const out = await runActions(valid, runCtx);
  assert.deepEqual(ran.map(a => a.type), ['proj', 'open_entity']);
  assert.equal(ran[1].zoom, 12.5);
  assert.equal(out.done.length, 3);
  assert.equal(describeActions(out.done, runCtx), 'Moved the map to 51.47° N, 0.45° W. Switched to the globe. Opened London Heathrow Airport.');
});

await Promise.all(asyncTests);
console.log(`ok ${passed} tests`);
