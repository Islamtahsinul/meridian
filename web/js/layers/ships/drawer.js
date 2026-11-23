// "Maritime" overview drawer: straits, IMF PortWatch chokepoints, busiest ports, anomaly candidates, dark ships,
// sanctioned vessels and sources.

import { classOf, hours } from './common.js';
import { openPortActivity } from './cards.js';
import { anomaliesTab, openAnomaly } from './anomalies.js';
import { chokepointsTab } from './portwatch.js';

const TABS = [
  { id: 'straits', label: 'Straits' },
  { id: 'chokepoints', label: 'Chokepoints' },
  { id: 'ports', label: 'Ports' },
  { id: 'anomalies', label: 'Anomalies' },
  { id: 'dark', label: 'Dark ships' },
  { id: 'sanctioned', label: 'Sanctioned' },
  { id: 'sources', label: 'Sources' },
];

export function overview(ctx, { straits, openEntity, trail = null, onState = null }) {
  const { h, fmt, api, ui } = ctx;
  let tab = ctx.settings.get('ships.tab', 'straits');
  let drawer = null;
  let timer = null;

  const tabBar = h('div', { class: 'ships-tabs', role: 'tablist' });
  const body = h('div', { class: 'ships-panel', role: 'tabpanel' });

  function renderTabs() {
    ctx.dom.clear(tabBar);
    for (const t of TABS) {
      const b = h('button', { type: 'button', role: 'tab', class: 'ships-tab', 'aria-selected': String(t.id === tab) }, t.label);
      b.addEventListener('click', () => { tab = t.id; ctx.settings.set('ships.tab', tab); renderTabs(); load(); });
      tabBar.append(b);
    }
  }

  function message(text) {
    ctx.dom.clear(body);
    body.append(h('p', { class: 'ships-empty' }, text));
  }

  function table(head, rows) {
    const tbl = h('table', { class: 'tbl ships-tbl' }, h('tr', null, head.map(([label, num]) => h('th', { class: num ? 'num' : null }, label))));
    for (const r of rows) tbl.append(r);
    return tbl;
  }

  function linkCell(text, sub, onClick) {
    const b = h('button', { type: 'button', class: 'text-btn ships-link' }, text);
    b.addEventListener('click', onClick);
    return h('td', null, b, sub ? h('div', { class: 'ships-sub' }, sub) : null);
  }

  async function loadStraits() {
    const list = await straits.load(null, true);
    ctx.dom.clear(body);
    const live = list.filter(s => s.coverage);
    body.append(h('p', { class: 'ships-note' }, live.length < list.length
      ? `${live.length} of ${list.length} straits have live coverage. ${straits.note() || ''}`.trim()
      : 'Every monitored strait has live coverage.'));
    const rows = [...list].sort((a, b) => (b.coverage - a.coverage) || (b.inside_now - a.inside_now)).map(s => h('tr', { class: s.coverage ? '' : 'ships-dim' },
      linkCell(s.name, s.congestion ? `Busier than usual: ${s.congestion}×` : (s.coverage ? (s.sources || []).join(', ') : 'No live coverage'),
        () => openEntity({ kind: 'strait', id: `ships:${s.id}`, title: s.name, lat: s.label[1], lon: s.label[0], layer: 'ships.straits', bbox: s.bbox })),
      h('td', { class: 'num' }, s.coverage ? fmt.int(s.inside_now) : '–'),
      h('td', { class: 'num' }, s.coverage ? fmt.int(s.transits_24h) : '–'),
      h('td', { class: 'num' }, s.coverage ? fmt.int(s.unique_24h) : '–')));
    body.append(table([['Strait'], ['Now', true], ['Transits 24 h', true], ['Vessels 24 h', true]], rows));
  }

  async function loadPorts() {
    const r = await api.get('/api/ships/ports', { limit: 25 });
    ctx.dom.clear(body);
    if (!r.ports.length) { message('No port has vessels reported right now.'); return; }
    body.append(h('p', { class: 'ships-note' }, `${r.note} ${r.source === 'infra' ? 'Ports from the infrastructure layer.' : 'Built-in list of major ports.'}`));
    const rows = r.ports.map(p => h('tr', null,
      linkCell(p.name, p.country_name || p.country || '', () => {
        ctx.map2.flyTo(p.lat, p.lon, 11);
        openPortActivity(ctx, p);
      }),
      h('td', { class: 'num' }, fmt.int(p.in_port)), h('td', { class: 'num' }, fmt.int(p.underway)),
      h('td', { class: 'num' }, fmt.int(p.arrivals_24h)), h('td', { class: 'num' }, fmt.int(p.departures_24h))));
    body.append(table([['Port'], ['In port', true], ['Under way', true], ['Arrived 24 h', true], ['Left 24 h', true]], rows));
  }

  async function loadDark() {
    const r = await api.get('/api/ships/dark');
    ctx.dom.clear(body);
    body.append(h('p', { class: 'ships-note' }, r.rule));
    if (!r.dark.length) { body.append(h('p', { class: 'ships-empty' }, 'No dark ships right now.')); return; }
    const rows = r.dark.map(d => h('tr', null,
      linkCell(d.name || `MMSI ${d.mmsi}`, [classOf(d.cls).label, d.flag_name].filter(Boolean).join(', '),
        () => openEntity({ kind: 'ship', id: d.mmsi, title: d.name, lat: d.last.lat, lon: d.last.lon, layer: 'ships.dark' })),
      h('td', { class: 'num' }, hours(d.silent_s)), h('td', { class: 'num' }, `${fmt.num(d.radius_km, 0)} km`)));
    body.append(table([['Vessel'], ['Silent', true], ['Uncertainty', true]], rows));
  }

  async function loadSanctioned() {
    const r = await api.get('/api/ships/sanctioned', { possible: 1 });
    ctx.dom.clear(body);
    body.append(h('p', { class: 'ships-note' }, r.note));
    if (!r.ready) { body.append(h('p', { class: 'ships-empty' }, 'The sanctions list is still loading.')); return; }
    if (!r.vessels.length) { body.append(h('p', { class: 'ships-empty' }, 'No listed vessels among the ships Meridian receives.')); return; }
    const rows = r.vessels.map(v => h('tr', { class: v.strength === 'confirmed' ? '' : 'ships-dim' },
      linkCell(v.name, `${v.strength === 'confirmed' ? `Listed as ${v.listed_name} (${(v.lists || []).join(', ') || 'sanctions lists'})` : 'Possible match on name or MMSI only'}${v.program ? `, ${v.program}` : ''}`,
        () => openEntity({ kind: 'ship', id: v.mmsi, title: v.name, lat: v.lat, lon: v.lon, layer: 'ships' })),
      h('td', null, v.flag || ''), h('td', { class: 'num' }, fmt.ago(v.t))));
    body.append(table([['Vessel'], ['Flag'], ['Seen', true]], rows));
  }

  async function loadSources() {
    const r = await api.get('/api/ships/status');
    ctx.dom.clear(body);
    body.append(h('div', { class: 'ships-stats' },
      stat(fmt.int(r.live), 'on the map'), stat(fmt.int(r.moving), 'moving'), stat(fmt.int(r.tracked), 'tracked, 7 days'), stat(fmt.int(r.dark), 'dark')));
    const rows = Object.entries(r.sources).map(([id, s]) => {
      const state = s.ok === true ? ['ok', 'Live'] : s.ok === false ? ['bad', 'Failing'] : ['', s.needs_key ? `Needs ${s.needs_key}` : 'Waiting'];
      return h('tr', null, h('td', null, s.name || id, s.note ? h('div', { class: 'ships-sub' }, s.note) : null, s.error ? h('div', { class: 'ships-sub ships-err' }, s.error) : null),
        h('td', null, h('span', { class: `state ${state[0]}` }, state[1])), h('td', { class: 'num' }, s.count != null ? fmt.int(s.count) : ''));
    });
    body.append(table([['Source'], ['State'], ['Vessels', true]], rows));
    if (r.gnss_hotspots?.length) {
      body.append(h('h3', { class: 'ships-h3' }, 'Possible GNSS interference'));
      for (const g of r.gnss_hotspots) {
        const what = g.max_sog ? `report up to ${fmt.int(g.max_sog)} kn` : `show ${(g.signatures || []).join(', ') || 'impossible positions'}`;
        const b = h('button', { type: 'button', class: 'text-btn ships-link' }, `${fmt.latlon(g.lat, g.lon, 2)}: ${g.vessels} vessels ${what}`);
        b.addEventListener('click', () => {
          ctx.map2.flyTo(g.lat, g.lon, 9);
          if (g.id) openAnomaly(ctx, { id: `gnss:${String(g.id).replace(/^ships:/, '')}` }, trail);
        });
        body.append(h('p', { class: 'ships-note' }, b));
      }
    }
    body.append(h('p', { class: 'ships-note' }, r.has_global
      ? 'aisstream.io adds community receivers worldwide; coverage still has gaps far from shore.'
      : 'Without an aisstream.io key Meridian sees Finnish waters and the northern Baltic only (Digitraffic). Add AISSTREAM_API_KEY to the .env file for global coverage.'));
  }

  function stat(value, label) {
    return h('div', null, h('span', { class: 'ships-stat-v' }, value), h('span', { class: 'ships-stat-k' }, label));
  }

  async function load() {
    if (!drawer) return;
    try {
      if (tab === 'straits') await loadStraits();
      else if (tab === 'chokepoints') await chokepointsTab(ctx, body, { openEntity, table, linkCell });
      else if (tab === 'anomalies') await anomaliesTab(ctx, body, { table, linkCell, trail });
      else if (tab === 'ports') await loadPorts();
      else if (tab === 'dark') await loadDark();
      else if (tab === 'sanctioned') await loadSanctioned();
      else await loadSources();
    } catch (err) {
      message(`This list is unavailable right now (${err?.message || 'no answer'}).`);
    }
  }

  return {
    toggle() {
      if (drawer) { drawer.close(); return; }
      renderTabs();
      message('Loading…');
      drawer = ui.drawer({ id: 'ships-overview', title: 'Maritime', el: h('div', { class: 'ships-drawer' }, tabBar, body), width: 440,
        onClose: () => { drawer = null; clearInterval(timer); timer = null; onState?.(false); } });
      onState?.(true);
      load();
      timer = setInterval(() => { if (document.visibilityState === 'visible') load(); }, 60000);
    },
    isOpen: () => !!drawer,
  };
}
