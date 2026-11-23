// Maritime anomalies: candidates, never accusations, from meridian/modules/ships/anomalies.py. Possible
// ship-to-ship transfers, loitering vessels, AIS gap episodes near straits and areas of interest, and possible
// GNSS spoofing of ships. The ships.anomalies layer, the anomaly card, the vessel card's section and the
// Maritime drawer tab.

import { AMBER, INK, PAPER, RED, circlePolygon } from './common.js';

const GREY = '#9aa6bd';
const ICON = '◈';
export const KINDS = [
  { id: 'sts', label: 'Ship-to-ship transfer candidates', short: 'Ship-to-ship', color: AMBER },
  { id: 'loitering', label: 'Loitering vessels', short: 'Loitering', color: AMBER },
  { id: 'ais_gap', label: 'AIS gaps near areas of interest', short: 'AIS gap', color: AMBER },
  { id: 'gnss', label: 'Possible GNSS spoofing', short: 'GNSS spoofing', color: RED },
];
const KIND = Object.fromEntries(KINDS.map(k => [k.id, k]));
const STATE_TEXT = {
  candidate: null, learning: 'Learning: not flagged yet', usual: 'Usual area: not flagged',
  possible_bunkering: 'Possible bunkering: not flagged', outside_areas: 'Outside areas of interest: not flagged',
  coverage_hole: 'Likely a coverage hole: not flagged',
};

function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

function sanctionBadges(a) {
  const listed = (a.vessels || []).filter(v => v.sanctions?.strength === 'listed');
  const possible = (a.vessels || []).filter(v => v.sanctions?.strength === 'possible');
  const out = [];
  if (listed.length) {
    const lists = [...new Set(listed.flatMap(v => v.sanctions.lists || []))];
    out.push({ text: `Involves a sanctioned vessel (${lists.join(', ')})`, tone: 'red', title: listed.map(v => `${v.title}: ${v.sanctions.text}`).join('; ') });
  } else if (possible.length) {
    out.push({ text: 'Possible sanctions match', tone: 'amber', title: 'A name or MMSI match only; the IMO does not match a listing' });
  }
  return out;
}

export function anomalyBadges(a) {
  const k = KIND[a.kind] || KINDS[0];
  const out = [{ text: k.short, tone: a.kind === 'gnss' ? 'red' : 'amber' }, { text: `${cap(a.confidence)} confidence`, tone: 'grey' }];
  if (STATE_TEXT[a.state]) out.push({ text: STATE_TEXT[a.state], tone: 'grey' });
  if (a.ongoing) out.push({ text: 'Ongoing', tone: 'blue' });
  return out.concat(sanctionBadges(a));
}

function vesselList(ctx, vessels) {
  const { h } = ctx;
  if (!vessels?.length) return h('p', { class: 'dim' }, 'No vessel details available.');
  const ul = h('ul', { class: 'card-list ships-anom-vessels' });
  for (const v of vessels) {
    const badges = [];
    if (v.sanctions?.strength === 'listed') badges.push(h('span', { class: 'badge red', title: v.sanctions.text }, `Sanctioned (${(v.sanctions.lists || []).join(', ')})`));
    else if (v.sanctions?.strength === 'possible') badges.push(h('span', { class: 'badge amber', title: v.sanctions.text }, 'Possible match'));
    const content = [h('span', { class: 'li-t' }, v.title, ...badges), h('span', { class: 'li-s' }, [v.subtitle?.replace(/, (sanctioned: [^,]+(, [A-Z]{2,4})*|possible sanctions match)$/, ''), v.role === 'vessel' ? null : v.role].filter(Boolean).join('; '))];
    const b = h('button', { type: 'button' }, content);
    b.addEventListener('click', () => ctx.entities.open({ kind: 'ship', id: v.id, title: v.title, lat: v.lat, lon: v.lon, layer: 'ships' }));
    ul.append(h('li', null, b));
  }
  return ul;
}

function bulletList(ctx, items) {
  const { h } = ctx;
  return h('ul', { class: 'ships-bullets' }, (items || []).map(t => h('li', null, t)));
}

function entityOf(a) {
  if (a.event_kind === 'gps_jam') return { kind: 'gps_jam', id: a.event_id, title: a.title, lat: a.lat, lon: a.lon, layer: 'ships.anomalies', zoom: 9 };
  const v = (a.vessels || []).find(x => x.id === a.event_id) || (a.vessels || [])[0];
  return { kind: 'ship', id: a.event_id, title: v?.title || a.title, subtitle: v?.subtitle, lat: v?.lat ?? a.lat, lon: v?.lon ?? a.lon, layer: 'ships.anomalies', zoom: 11 };
}

export function anomalySpec(ctx, a, trail) {
  const { fmt } = ctx;
  const sections = [
    { title: 'What was seen', text: a.observed },
    { title: 'Evidence', rows: a.evidence || [] },
    { title: 'Vessels involved', el: vesselList(ctx, a.vessels) },
    { title: 'How confident', text: `${cap(a.confidence)}. ${a.confidence_text || ''}`.trim() },
    { title: 'Coverage', text: a.coverage },
    { title: 'Why this may be innocent', el: bulletList(ctx, a.innocent) },
    { title: 'Limits', text: a.limits },
  ];
  if (a.aoi) sections.push({ title: 'Area of interest', text: `${cap(a.aoi.name)}${a.aoi.basis ? `: ${a.aoi.basis}` : ''} Being in such an area does not make a vessel suspect; it only adds context.` });
  const ships = (a.vessels || []).filter(v => v.kind === 'ship' && /^\d{9}$/.test(v.id)).slice(0, 4);
  const actions = [];
  if (trail && ships.length && a.kind !== 'gnss') {
    const on = ships.some(v => trail.isOn(v.id));
    actions.push({ text: on ? 'Hide trails' : 'Show trails', primary: !on, onClick: async (card) => {
      for (const v of ships) {
        if (on) trail.hide(v.id); else if (!trail.isOn(v.id)) await trail.show(v.id, v.sanctions?.strength === 'listed' ? RED : AMBER);
      }
      card.update({ actions: anomalySpec(ctx, a, trail).actions });
    } });
  }
  return {
    key: `ships-anomaly:${a.id}`, kind: a.event_kind === 'gps_jam' ? 'gps_jam' : 'ship', title: a.title, subtitle: a.subtitle, icon: ICON,
    accent: a.kind === 'gnss' ? RED : AMBER, badges: anomalyBadges(a),
    block: (a.block || []).map(([label, value]) => ({ label, value })),
    sections, actions, entity: entityOf(a),
    foot: `A candidate, not an accusation. ${a.ongoing ? 'Still going on' : `Last seen ${fmt.ago(a.time)}`}; first seen ${fmt.utc(a.first_time)}.`,
  };
}

export async function openAnomaly(ctx, ref, trail) {
  const id = String(ref.id || ref.anomaly_id || '');
  const placeholder = { key: `ships-anomaly:${id}`, kind: 'ship', title: ref.title || 'Maritime anomaly', subtitle: 'Loading the evidence…', icon: ICON, accent: AMBER };
  if (!/^(sts|loiter|gap|gnss):[0-9A-Za-z.,:_-]{1,80}$/.test(id)) return ctx.cards.open({ ...placeholder, subtitle: 'Unknown anomaly' });
  const card = ctx.cards.open(placeholder);
  let a;
  try {
    a = await ctx.api.get(`/api/ships/anomalies/${encodeURIComponent(id)}`);
  } catch (err) {
    card.update({ subtitle: err?.status === 404 ? 'No longer in the last 30 days of anomalies.' : 'The anomaly details are unavailable right now.' });
    return card;
  }
  if (!card.isOpen()) return card;
  const spec = anomalySpec(ctx, a, trail);
  if (a.ongoing) {
    spec.live = { everyMs: 60000, update: async () => {
      const next = anomalySpec(ctx, await ctx.api.get(`/api/ships/anomalies/${encodeURIComponent(id)}`), trail);
      return { title: next.title, subtitle: next.subtitle, badges: next.badges, block: next.block, sections: next.sections, foot: next.foot, entity: next.entity };
    } };
  }
  return ctx.cards.open(spec);
}

/** The ship card's section: anomalies involving this vessel (flagged or not). */
export function vesselSection(ctx, anomalies, trail) {
  if (!anomalies?.length) return null;
  return { title: 'Anomalies', list: anomalies.map(a => ({
    title: a.title, subtitle: [a.subtitle, STATE_TEXT[a.state]].filter(Boolean).join('. '),
    detail: a.ongoing ? 'now' : ctx.fmt.ago(a.time), onClick: () => openAnomaly(ctx, a, trail),
  })) };
}

// ------------------------------------------------------------------ the layer
export function anomaliesLayer(ctx, { trail }) {
  const { map2, api, layers, fmt } = ctx;
  const SRC = 'ov-ships-anom';
  const IDS = { fill: 'ov-ships-anom-fill', ring: 'ov-ships-anom-ring', line: 'ov-ships-anom-line', gap: 'ov-ships-anom-gap',
    pt: 'ov-ships-anom-pt', label: 'ov-ships-anom-label' };
  let items = [];

  function features(list) {
    const feats = [];
    for (const a of list) {
      const color = !a.published ? GREY : a.kind === 'gnss' ? RED : AMBER;
      const short = (KIND[a.kind] || KINDS[0]).short;
      const props = { id: a.id, kind: a.kind, title: a.title, sub: a.subtitle, pub: a.published ? 1 : 0, c: color, sev: a.severity,
        label: `${short}${a.published ? '' : ' (not flagged)'}` };
      const g = a.geometry;
      if (a.kind === 'sts' && g?.type === 'LineString') feats.push({ type: 'Feature', geometry: g, properties: { ...props, part: 'pair' } });
      if (a.kind === 'loitering') {
        if (g?.type === 'LineString' && g.coordinates.length > 1) feats.push({ type: 'Feature', geometry: g, properties: { ...props, part: 'track' } });
        feats.push({ type: 'Feature', geometry: circlePolygon(a.lat, a.lon, Math.max(0.6, a.radius_km || 1)), properties: { ...props, part: 'area' } });
      }
      if (a.kind === 'ais_gap' && g?.type === 'LineString') {
        feats.push({ type: 'Feature', geometry: g, properties: { ...props, part: 'gap' } });
        feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: g.coordinates[1] }, properties: { ...props, part: 'end' } });
      }
      if (a.kind === 'gnss') feats.push({ type: 'Feature', geometry: circlePolygon(a.lat, a.lon, a.radius_km || 5), properties: { ...props, part: 'area' } });
      feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [a.lon, a.lat] }, properties: { ...props, part: 'pt' } });
    }
    return { type: 'FeatureCollection', features: feats };
  }

  const clickable = [IDS.pt, IDS.line, IDS.gap, IDS.ring, IDS.label];
  map2.onClick(clickable, (f) => openAnomaly(ctx, { id: f.properties.id, title: f.properties.title }, trail));
  map2.onHover(clickable, (f) => `${f.properties.title}: ${f.properties.sub}${f.properties.pub ? '' : ' (not flagged)'}`);

  const handle = layers.register({
    id: 'ships.anomalies', group: 'Ships', title: 'Maritime anomalies', subtitle: 'Candidates for a closer look, never accusations',
    description: 'Possible ship-to-ship transfers in open water, vessels loitering away from anchorages, AIS gaps near straits and areas of interest, and possible GNSS spoofing of ships. Ports, anchorages, usual waiting areas, pilot areas and offshore installations are excluded. Each card says what was seen, how confident it is and why it may be innocent.',
    source: 'Derived by Meridian from the AIS feeds; coastlines from Natural Earth', color: AMBER, symbol: 'point', refreshMs: 60000,
    legend: [{ color: AMBER, label: 'Ship-to-ship candidate (line joins the pair)', shape: 'line' }, { color: AMBER, label: 'Loitering area', shape: 'area' },
      { color: AMBER, label: 'AIS gap (dotted: silent stretch)', shape: 'line' }, { color: RED, label: 'Possible GNSS spoofing', shape: 'area' },
      { color: GREY, label: 'Shown but not flagged' }],
    options: [
      { id: 'sts', label: 'Ship-to-ship candidates', type: 'toggle', default: true },
      { id: 'loitering', label: 'Loitering', type: 'toggle', default: true },
      { id: 'ais_gap', label: 'AIS gaps', type: 'toggle', default: true },
      { id: 'gnss', label: 'GNSS spoofing', type: 'toggle', default: true },
      { id: 'unflagged', label: 'Also show what was not flagged', type: 'toggle', default: false },
    ],
    async enable() {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: IDS.fill, type: 'fill', source: SRC, filter: ['==', ['get', 'part'], 'area'], paint: { 'fill-color': ['get', 'c'], 'fill-opacity': ['case', ['==', ['get', 'pub'], 1], 0.12, 0.05] } }, 'polygons');
      map2.addLayer({ id: IDS.ring, type: 'line', source: SRC, filter: ['==', ['get', 'part'], 'area'], paint: { 'line-color': ['get', 'c'], 'line-width': 1.3, 'line-dasharray': [2, 2], 'line-opacity': 0.9 } }, 'lines');
      map2.addLayer({ id: IDS.line, type: 'line', source: SRC, filter: ['in', ['get', 'part'], ['literal', ['pair', 'track']]], layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': ['get', 'c'], 'line-width': ['case', ['==', ['get', 'part'], 'pair'], 3, 1.4], 'line-opacity': ['case', ['==', ['get', 'part'], 'pair'], 0.95, 0.7] } }, 'lines');
      map2.addLayer({ id: IDS.gap, type: 'line', source: SRC, filter: ['==', ['get', 'part'], 'gap'], layout: { 'line-cap': 'round' },
        paint: { 'line-color': ['get', 'c'], 'line-width': 2, 'line-dasharray': [0.5, 2], 'line-opacity': 0.9 } }, 'lines');
      map2.addLayer({ id: IDS.pt, type: 'circle', source: SRC, filter: ['in', ['get', 'part'], ['literal', ['pt', 'end']]], paint: {
        'circle-radius': ['case', ['==', ['get', 'part'], 'end'], 3.5, ['==', ['get', 'pub'], 1], 6, 4.5],
        'circle-color': ['case', ['==', ['get', 'part'], 'end'], INK, ['get', 'c']], 'circle-stroke-color': ['case', ['==', ['get', 'part'], 'end'], ['get', 'c'], INK],
        'circle-stroke-width': ['case', ['==', ['get', 'part'], 'end'], 1.6, 1.4] } }, 'top');
      map2.addLayer({ id: IDS.label, type: 'symbol', source: SRC, minzoom: 4, filter: ['==', ['get', 'part'], 'pt'], layout: {
        'text-field': ['get', 'label'], 'text-font': ['Open Sans Regular'], 'text-size': 11, 'text-offset': [0, 1.1], 'text-anchor': 'top', 'text-optional': true,
      }, paint: { 'text-color': ['case', ['==', ['get', 'pub'], 1], '#ffe7bf', PAPER], 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'top');
    },
    async disable() { map2.removeSource(SRC); },
    async refresh({ signal, options }) {
      const kinds = KINDS.filter(k => options[k.id] !== false).map(k => k.id);
      if (!kinds.length) { items = []; map2.setData(SRC, map2.emptyFC()); handle.setCount(0); handle.setStatus('ok', 'Every kind of anomaly is switched off in the layer options.'); return; }
      const r = await api.get('/api/ships/anomalies', { kinds: kinds.join(','), all: options.unflagged ? 1 : null, hours: 48 }, { signal });
      items = r.anomalies || [];
      map2.setData(SRC, features(items));
      handle.setCount(r.flagged);
      const learning = Object.entries(r.detectors || {}).filter(([, d]) => d.state === 'learning').map(([k, d]) => `${(KIND[k] || {}).short || k}: ${d.status}`);
      if (!items.length) handle.setStatus('ok', ['No candidates in the last 48 hours.', ...learning].join(' '));
      else handle.setStatus('ok', learning.length ? learning.join(' ') : null);
    },
  });
  return { handle, items: () => items };
}

// ------------------------------------------------------------------ Maritime drawer tab
export async function anomaliesTab(ctx, body, { table, linkCell, trail }) {
  const { h, fmt, api } = ctx;
  const r = await api.get('/api/ships/anomalies', { hours: 48 });
  ctx.dom.clear(body);
  body.append(h('p', { class: 'ships-note' }, r.note));
  for (const [k, d] of Object.entries(r.detectors || {})) {
    if (d.state !== 'ready' || d.error) body.append(h('p', { class: 'ships-note' }, `${(KIND[k] || {}).label || k}: ${d.error ? `failing (${d.error})` : d.status}`));
    if (d.excluded_waters) body.append(h('p', { class: 'ships-note' }, `${(KIND[k] || {}).short || k}: ${d.excluded_waters}`));
  }
  if (!r.anomalies.length) { body.append(h('p', { class: 'ships-empty' }, 'No flagged candidates in the last 48 hours.')); return; }
  const rows = r.anomalies.map(a => h('tr', null,
    linkCell(a.title, a.subtitle, () => {
      ctx.layers.setOn('ships.anomalies', true);
      ctx.map2.flyTo(a.lat, a.lon, a.kind === 'gnss' ? 8 : 10);
      openAnomaly(ctx, a, trail);
    }),
    h('td', null, cap(a.confidence)),
    h('td', { class: 'num' }, a.ongoing ? 'now' : fmt.ago(a.time))));
  body.append(table([['Candidate'], ['Confidence'], ['Seen', true]], rows));
}

