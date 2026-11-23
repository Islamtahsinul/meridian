// node tests/test_core_layers.mjs : checks ctx.layers.resume() (web/js/core/layers.js) without a browser.
// resume() must switch every suspended layer back on together, so a slow source never holds back the others.
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('../web/js/core/layers.js', import.meta.url), 'utf8');
const start = src.indexOf('export async function resume');
const end = src.indexOf('export function isSuspended');
if (start < 0 || end < 0) { console.log('FAIL resume() not found in layers.js'); process.exit(1); }
const body = src.slice(start, end).replace('export async function resume', 'async function resume');

let failed = 0;
const check = (ok, msg) => { if (!ok) { failed++; console.log(`FAIL ${msg}`); } };

const suspended = new Set(['flights', 'ships', 'broken']);
const on = new Set();
const pending = new Map();
let persisted = 0;
async function setOn(id, value) {
  on.add(id);                                           // entry.on flips at once in the real setOn
  if (id === 'broken') throw new Error('enable failed');
  await new Promise(resolve => pending.set(id, resolve));   // waits for "the network"
}
const persistOn = () => { persisted++; };
const errors = [];
const console2 = { error: (...a) => errors.push(a.join(' ')) };
const resume = new Function('suspended', 'setOn', 'persistOn', 'console', `${body}; return resume;`)(
  suspended, setOn, persistOn, console2);

const done = resume();
await new Promise(r => setTimeout(r, 10));
check(on.has('flights') && on.has('ships') && on.has('broken'), 'every layer is switched on before any finishes loading');
check(suspended.size === 0, 'nothing stays suspended');
check(persisted === 0, 'the layer set is saved once, after the layers are back');
pending.get('ships')();                                  // ships finishes first; flights is still loading
await new Promise(r => setTimeout(r, 10));
pending.get('flights')();
const list = await done;
check(JSON.stringify(list) === JSON.stringify(['flights', 'ships', 'broken']), `resume returns the restored ids (${list})`);
check(persisted === 1, 'saved once');
check(errors.length === 1 && errors[0].includes('broken'), 'a failing layer is logged and does not stop the others');

const tests = 6;
console.log(failed ? `${failed} failed` : `ok ${tests} layer resume tests`);
if (failed) process.exit(1);
