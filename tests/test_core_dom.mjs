// node tests/test_core_dom.mjs : checks core URL safety (web/js/core/dom.js safeUrl) without a browser.
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('../web/js/core/dom.js', import.meta.url), 'utf8');
const SAFE = new Function(`return ${src.match(/const SAFE_URL = (.*);/)[1]}`)();
const UNSAFE = new Function(`return ${src.match(/const UNSAFE_CHARS = (\/.*\/);/)[1]}`)();
const safeUrl = (url) => { const s = String(url ?? '').trim(); if (UNSAFE.test(s)) return null; return SAFE.test(s) ? s : null; };

const cases = [
  ['https://example.org/a?b=1', true], ['http://example.org', true], ['/api/core/manifest', true],
  ['./x.png', true], ['#top', true], ['data:image/png;base64,AAAA', true], ['blob:http://127.0.0.1/x', true],
  ['javascript:alert(1)', false], [' JavaScript:alert(1)', false], ['data:text/html,<b>x</b>', false],
  ['//evil.example', false], ['/\\evil.example', false], ['/\tevil.example', false], ['/\nevil', false],
  ['vbscript:x', false], ['', false], [null, false],
];
let failed = 0;
for (const [input, ok] of cases) {
  const got = safeUrl(input) !== null;
  if (got !== ok) { failed++; console.log(`FAIL ${JSON.stringify(input)}: expected ${ok ? 'allowed' : 'refused'}`); }
}
console.log(failed ? `${failed} failed` : `ok ${cases.length} tests`);
if (failed) process.exit(1);

// kindLabel (sentence-case names for entity kinds)
{
  const start = src.indexOf('const KIND_LABELS');
  const end = src.indexOf('export function clear');
  const body = src.slice(start, end).replace('export function kindLabel', 'function kindLabel');
  const kindLabel = new Function(`${body}; return kindLabel;`)();
  const expect = [
    [['power_plant'], 'Power plant'], [['company', true], 'Companies'], [['volcano', true], 'Volcanoes'],
    [['launch', true], 'Launches'], [['aircraft', true], 'Aircraft'], [['news', true], 'News'],
    [['datacenter', true], 'Data centres'], [['country', true], 'Countries'], [['mystery_kind'], 'Mystery kind'],
  ];
  let bad = 0;
  for (const [args, want] of expect) {
    const got = kindLabel(...args);
    if (got !== want) { bad++; console.log(`FAIL kindLabel(${args.join(', ')}) = ${got}, want ${want}`); }
  }
  console.log(bad ? `${bad} kindLabel failures` : `ok ${expect.length} kindLabel tests`);
  if (bad) process.exit(1);
}
