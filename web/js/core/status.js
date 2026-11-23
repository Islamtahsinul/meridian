// Sources and status, the layer table, keys and shortcuts (CONTRACT.md section 10.13).

import { h, clear, fmt } from './dom.js';
import { api } from './api.js';
import { ui } from './ui.js';
import { layers, GROUPS } from './layers.js';

let last = null;

export function startStatusPolling() {
  const btn = document.getElementById('status-summary');
  btn.addEventListener('click', openStatus);
  const tick = async () => {
    try {
      last = await api.get('/api/core/status');
      const srcs = last.sources.filter(s => s.ok !== null && s.ok !== undefined && !(s.optional && s.ok === false));
      const bad = srcs.filter(s => s.ok === false);
      const building = last.datasets.filter(d => d.building).length;
      const broken = last.modules.filter(m => !m.ok);
      clear(btn);
      btn.className = `sb-item sb-btn ${broken.length || bad.length > 3 ? 'bad' : bad.length ? 'warn' : ''}`;
      const parts = [`${srcs.length - bad.length} of ${srcs.length} sources live`];
      if (building) parts.push(`${building} dataset${building > 1 ? 's' : ''} building`);
      if (broken.length) parts.push(`${broken.length} module${broken.length > 1 ? 's' : ''} failed`);
      btn.append(h('span', { class: 'dot' }), parts.join(', '));
    } catch {
      clear(btn);
      btn.className = 'sb-item sb-btn bad';
      btn.append(h('span', { class: 'dot' }), 'Meridian server unreachable');
    }
  };
  tick();
  setInterval(() => { if (document.visibilityState === 'visible') tick(); }, 30000);
}

function stateCell(ok, text) {
  const cls = ok === true ? 'ok' : ok === false ? 'bad' : '';
  return h('span', { class: `state ${cls}` }, text);
}

export async function openStatus() {
  const body = h('div', null, h('p', { class: 'dim' }, 'Loading…'));
  const m = ui.modal({ title: 'Sources and status', body, wide: true });
  try { last = await api.get('/api/core/status'); } catch { m.setBody(h('p', { class: 'empty' }, 'The Meridian server is not answering. Is server.py still running?')); return; }
  const s = last;
  const wrap = h('div');
  const broken = s.modules.filter(x => !x.ok);
  if (broken.length) {
    wrap.append(h('h3', null, 'Modules that failed to load'));
    for (const b of broken) wrap.append(h('p', null, h('strong', null, b.name), ': ', b.error || 'unknown error'));
  }
  wrap.append(h('h3', null, 'Live sources'));
  const t1 = h('table', { class: 'tbl' }, h('tr', null, h('th', null, 'Source'), h('th', null, 'Module'), h('th', null, 'State'), h('th', { class: 'num' }, 'Items'), h('th', null, 'Updated'), h('th', null, 'Licence')));
  for (const src of s.sources) {
    const st = src.ok === true ? stateCell(true, 'Live') : src.ok === false ? stateCell(src.optional ? null : false, `${src.optional ? 'Optional, ' : ''}${src.error || 'failing'}`) : stateCell(null, src.needs_key ? `Needs ${src.needs_key}` : (src.optional ? 'Optional, off' : 'Waiting'));
    t1.append(h('tr', null,
      h('td', null, src.url ? h('a', { href: src.url }, src.name || src.id) : (src.name || src.id), src.note ? h('div', { class: 'faint', style: { fontSize: '12px' } }, src.note) : null, src.refresh ? h('div', { class: 'faint', style: { fontSize: '12px' } }, `Refresh: ${src.refresh}`) : null),
      h('td', { class: 'dim' }, src.module), h('td', null, st), h('td', { class: 'num' }, src.count != null ? fmt.int(src.count) : ''),
      h('td', { class: 'dim' }, src.updated_at ? fmt.ago(src.updated_at) : ''), h('td', { class: 'dim', style: { fontSize: '12px' } }, src.licence || '')));
  }
  wrap.append(t1);
  wrap.append(h('h3', { style: { marginTop: '18px' } }, 'Datasets'));
  const t2 = h('table', { class: 'tbl' }, h('tr', null, h('th', null, 'Dataset'), h('th', null, 'State'), h('th', { class: 'num' }, 'Rows'), h('th', null, 'Built'), h('th', null, 'Notes')));
  for (const d of s.datasets) {
    const st = d.building ? stateCell(null, 'Building…') : d.ready ? stateCell(d.last_error ? null : true, d.last_error ? 'Ready (last refresh failed)' : 'Ready') : stateCell(false, d.last_error || 'Not built yet');
    t2.append(h('tr', null, h('td', null, d.key, h('div', { class: 'faint', style: { fontSize: '12px' } }, d.description)), h('td', null, st),
      h('td', { class: 'num' }, d.rows != null ? fmt.int(d.rows) : ''), h('td', { class: 'dim' }, d.built_at ? fmt.ago(d.built_at) : ''),
      h('td', { class: 'dim', style: { fontSize: '12px' } }, [...(d.notes || [])].join('; '))));
  }
  wrap.append(t2);
  wrap.append(h('h3', { style: { marginTop: '18px' } }, 'Optional keys'));
  wrap.append(h('p', { class: 'dim' }, 'Keys live in the .env file next to server.py and never reach the browser. Restart the server after editing it.'));
  const t3 = h('table', { class: 'tbl' }, h('tr', null, h('th', null, 'Setting'), h('th', null, 'What it adds'), h('th', null, 'State')));
  for (const k of s.keys) {
    t3.append(h('tr', null, h('td', null, h('code', null, k.name)), h('td', null, k.description, k.url ? h('div', null, h('a', { href: k.url }, 'Get one')) : null),
      h('td', null, k.secret ? stateCell(k.configured, k.configured ? 'Set' : 'Not set') : stateCell(k.configured ? true : null, k.configured ? String(k.value) : 'Default'))));
  }
  wrap.append(t3);
  wrap.append(h('h3', { style: { marginTop: '18px' } }, 'Background tasks'));
  const t4 = h('table', { class: 'tbl' }, h('tr', null, h('th', null, 'Task'), h('th', { class: 'num' }, 'Runs'), h('th', { class: 'num' }, 'Failures'), h('th', null, 'Last success'), h('th', null, 'Last error')));
  for (const t of s.tasks) {
    t4.append(h('tr', null, h('td', null, t.name, t.every ? h('span', { class: 'faint' }, ` every ${fmt.int(t.every)} s`) : null), h('td', { class: 'num' }, fmt.int(t.runs)),
      h('td', { class: 'num' }, fmt.int(t.failures)), h('td', { class: 'dim' }, t.last_ok ? fmt.ago(t.last_ok) : ''), h('td', { class: 'dim', style: { fontSize: '12px' } }, t.last_error || '')));
  }
  wrap.append(t4);
  const hist = s.history;
  if (hist) {
    const snap = hist.last_snapshot;
    wrap.append(h('h3', { style: { marginTop: '18px' } }, 'Event history'));
    wrap.append(h('p', { class: 'dim' }, hist.rows != null ? `${fmt.int(hist.rows)} events kept for ${fmt.int(hist.retention_days)} days.` : `Events are kept for ${fmt.int(hist.retention_days)} days.`,
      ' ', snap ? `Last snapshot ${fmt.ago(snap.at)}: ${fmt.int(snap.events)} events, ${fmt.int(snap.new)} new.` : hist.scheduled ? 'The first snapshot runs a minute after start-up.' : 'Snapshots are off (background tasks are off).'));
  }
  m.setBody(wrap);
}

export async function openLayerTable() {
  const body = h('div', null, h('p', { class: 'dim' }, 'Loading…'));
  const m = ui.modal({ title: 'Layer table', body, wide: true });
  let server = [];
  try { server = (await api.get('/api/core/layers')).layers || []; } catch { server = []; }
  const byId = new Map(server.map(s => [s.id, s]));
  const rows = layers.list();
  const tbl = h('table', { class: 'tbl' }, h('tr', null, h('th', null, 'Group'), h('th', null, 'Layer'), h('th', { class: 'num' }, 'Count'), h('th', null, 'Source'), h('th', null, 'Refresh'), h('th', null, 'Good for')));
  const groupOrder = GROUPS.map(g => g.id);
  rows.sort((a, b) => (groupOrder.indexOf(a.group) - groupOrder.indexOf(b.group)) || a.title.localeCompare(b.title));
  for (const r of rows) {
    const s = byId.get(r.id) || {};
    const toggle = h('button', { class: 'text-btn', type: 'button', title: r.on ? 'Turn off' : 'Turn on' }, r.title);
    toggle.addEventListener('click', () => { layers.setOn(r.id, !layers.isOn(r.id)); m.close(); });
    const count = r.count != null ? r.count : s.count;
    tbl.append(h('tr', null, h('td', { class: 'dim' }, r.group), h('td', null, toggle, r.on ? h('span', { class: 'faint' }, ' (on)') : null),
      h('td', { class: 'num' }, count != null ? (typeof count === 'number' ? fmt.int(count) : String(count)) : ''),
      h('td', { class: 'dim', style: { fontSize: '12px' } }, s.source || r.source || ''), h('td', { class: 'dim', style: { fontSize: '12px' } }, s.refresh || (r.refreshMs ? `every ${Math.round(r.refreshMs / 1000)} s` : '')),
      h('td', { class: 'dim', style: { fontSize: '12px' } }, s.use || r.description || '')));
  }
  m.setBody(h('div', null, h('p', { class: 'dim' }, 'Every layer, what feeds it and what it is good for. Click a name to turn it on or off.'), tbl));
}

export function openShortcuts() {
  const list = ui.listShortcuts();
  const tbl = h('table', { class: 'tbl' }, h('tr', null, h('th', null, 'Key'), h('th', null, 'Action')));
  for (const s of list) tbl.append(h('tr', null, h('td', null, h('kbd', null, s.key)), h('td', null, s.description)));
  tbl.append(h('tr', null, h('td', null, 'Right-click'), h('td', null, "What's here? for any point on the map")));
  tbl.append(h('tr', null, h('td', null, h('kbd', null, 'Enter')), h('td', null, 'While drawing an area: finish it')));
  tbl.append(h('tr', null, h('td', null, h('kbd', null, 'Backspace')), h('td', null, 'While drawing an area: remove the last point')));
  tbl.append(h('tr', null, h('td', null, h('kbd', null, 'Esc')), h('td', null, 'While drawing an area: cancel')));
  ui.modal({ title: 'Keyboard shortcuts', body: tbl });
}
