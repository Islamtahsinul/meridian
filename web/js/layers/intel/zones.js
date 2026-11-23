// Risk zones (conflict clusters over 7 days) and convergences (3+ kinds of event within 24 h).

import { INK, INTEL, RISK_RAMP, ALERT_AMBER, sevBadge, safeGet, errorText, short } from './common.js';
import { openConflict } from './conflict.js';

const R_SRC = 'ov-intel-risk';
const R_CELL = 'ov-intel-risk-cell';
const R_LINE = 'ov-intel-risk-line';
const R_LABEL = 'ov-intel-risk-label';
const R_DOT = 'ov-intel-risk-dot';
const C_SRC = 'ov-intel-conv';
const C_BOX = 'ov-intel-conv-box';
const C_RING = 'ov-intel-conv-ring';
const C_DOT = 'ov-intel-conv-dot';
const C_LABEL = 'ov-intel-conv-label';
const LEVEL_COLOUR = { Elevated: RISK_RAMP[2], High: RISK_RAMP[3], 'Very high': RISK_RAMP[4] };
// Event kinds as sentence-case labels ("gps_jam" -> "GPS interference"); unknown kinds fall back to the id.
const KIND_LABEL = { gps_jam: 'GPS interference', conflict_event: 'Conflict event', earthquake: 'Earthquake', aircraft: 'Aircraft',
  ship: 'Ship', fire: 'Fire', storm: 'Storm', warning: 'Weather warning', volcano: 'Volcano', disaster: 'Disaster', flood: 'Flood',
  outage: 'Internet outage', cyber: 'Cyber', launch: 'Launch', news: 'News' };
const kindLabel = (k) => KIND_LABEL[k] || (k ? k.charAt(0).toUpperCase() + k.slice(1).replace(/_/g, ' ') : '');

export function registerZones(ctx) {
  const { map2, fmt } = ctx;

  // ------------------------------------------------------------------ risk zones
  const risk = ctx.layers.register({
    id: 'intel.risk', group: 'Intelligence', title: 'Risk zones', subtitle: 'Where conflict events cluster, 7 days',
    description: 'Half-degree cells where reported conflict events pile up (density times severity, recent days count more). Joined cells form a zone.',
    source: 'Derived from GDELT conflict events', color: RISK_RAMP[3], symbol: 'area',
    defaultOn: false, refreshMs: 10 * 60 * 1000,
    legend: [{ color: RISK_RAMP[2], label: 'Elevated', shape: 'area' }, { color: RISK_RAMP[3], label: 'High', shape: 'area' },
      { color: RISK_RAMP[4], label: 'Very high', shape: 'area' }],
    enable() {
      map2.addSource(R_SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: R_CELL, type: 'fill', source: R_SRC, filter: ['==', ['get', 'role'], 'cell'],
        paint: { 'fill-color': ['interpolate', ['linear'], ['get', 'w'], 0, RISK_RAMP[0], 0.35, RISK_RAMP[1], 0.6, RISK_RAMP[2], 0.85, RISK_RAMP[3], 1, RISK_RAMP[4]],
          'fill-opacity': ['interpolate', ['linear'], ['get', 'w'], 0, 0.1, 1, 0.33] } }, 'polygons');
      map2.addLayer({ id: R_LINE, type: 'line', source: R_SRC, filter: ['==', ['get', 'role'], 'outline'],
        layout: { 'line-join': 'round' },
        paint: { 'line-color': ['match', ['get', 'level'], 'Very high', RISK_RAMP[4], 'High', RISK_RAMP[3], RISK_RAMP[2]],
          'line-width': ['interpolate', ['linear'], ['zoom'], 2, 0.8, 8, 1.8] } }, 'lines');
      map2.addLayer({ id: R_DOT, type: 'circle', source: R_SRC, maxzoom: 5, filter: ['==', ['get', 'role'], 'label'],
        paint: { 'circle-radius': ['interpolate', ['linear'], ['get', 'score'], 2, 5, 25, 11], 'circle-color': 'rgba(255,224,122,0.18)',
          'circle-stroke-color': ['match', ['get', 'level'], 'Very high', RISK_RAMP[4], 'High', RISK_RAMP[3], RISK_RAMP[2]], 'circle-stroke-width': 1.6 } }, 'points');
      map2.addLayer({ id: R_LABEL, type: 'symbol', source: R_SRC, minzoom: 3.5, filter: ['==', ['get', 'role'], 'label'],
        layout: { 'text-field': ['format', ['get', 'name'], {}, '\n', {}, ['concat', ['get', 'level'], ' risk'], { 'font-scale': 0.85 }],
          'text-size': 12, 'text-font': ['Open Sans Bold'], 'symbol-sort-key': ['-', 0, ['get', 'score']], 'text-max-width': 12 },
        paint: { 'text-color': '#fff1b8', 'text-halo-color': INK, 'text-halo-width': 1.6 } }, 'top');
    },
    disable() { map2.removeSource(R_SRC); },
    async refresh({ signal }) {
      const fc = await ctx.api.get('/api/intel/risk', null, { signal });
      map2.setData(R_SRC, fc);
      const n = fc.meta?.zones || 0;
      risk.setCount(n);
      risk.setStatus('ok', n ? null : 'No clusters yet. Zones appear once enough conflict events are loaded.');
    },
  });
  map2.onClick([R_CELL, R_LINE, R_LABEL, R_DOT], (f) => openZone(ctx, { kind: 'risk_zone', id: f.properties.zone }));
  map2.onHover([R_CELL, R_LINE, R_LABEL, R_DOT], f => (f.properties.role === 'label' ? `${f.properties.name}: ${f.properties.level.toLowerCase()} risk, ${f.properties.events} events`
    : `${f.properties.level} risk zone${f.properties.n ? `, ${f.properties.n} events in this cell` : ''}`));
  ctx.entities.handler('risk_zone', (e) => openZone(ctx, e), { group: 'Intelligence' });

  // ------------------------------------------------------------------ convergences
  const conv = ctx.layers.register({
    id: 'intel.convergences', group: 'Intelligence', title: 'Convergences', subtitle: '3 or more kinds of event within 24 hours',
    description: 'Places where events of at least three different kinds (from every layer: conflict, hazards, aviation, shipping, cyber and others) coincide within about 100 km and 24 hours.',
    source: 'Derived from every module’s events', color: ALERT_AMBER, symbol: 'point',
    defaultOn: false, refreshMs: 5 * 60 * 1000,
    legend: [{ color: ALERT_AMBER, label: 'Convergence (ring size grows with severity)' }],
    enable() {
      map2.addSource(C_SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: C_BOX, type: 'line', source: C_SRC, minzoom: 5, filter: ['==', ['get', 'role'], 'box'],
        paint: { 'line-color': ALERT_AMBER, 'line-width': 1, 'line-dasharray': [2, 2], 'line-opacity': 0.7 } }, 'lines');
      map2.addLayer({ id: C_RING, type: 'circle', source: C_SRC, filter: ['!=', ['get', 'role'], 'box'],
        paint: { 'circle-radius': ['interpolate', ['linear'], ['get', 's'], 3, 9, 10, 16], 'circle-color': 'rgba(255,181,71,0.12)',
          'circle-stroke-color': ALERT_AMBER, 'circle-stroke-width': 2, 'circle-pitch-alignment': 'map' } }, 'top');
      map2.addLayer({ id: C_DOT, type: 'circle', source: C_SRC, filter: ['!=', ['get', 'role'], 'box'],
        paint: { 'circle-radius': 3, 'circle-color': ALERT_AMBER, 'circle-stroke-color': INK, 'circle-stroke-width': 1 } }, 'top');
      map2.addLayer({ id: C_LABEL, type: 'symbol', source: C_SRC, filter: ['!=', ['get', 'role'], 'box'],
        layout: { 'text-field': ['get', 'label'], 'text-size': 11, 'text-font': ['Open Sans Bold'], 'text-offset': [0, 1.7], 'text-anchor': 'top' },
        paint: { 'text-color': '#ffd99a', 'text-halo-color': INK, 'text-halo-width': 1.5 } }, 'top');
    },
    disable() { map2.removeSource(C_SRC); },
    async refresh({ signal }) {
      const fc = await ctx.api.get('/api/intel/convergences', null, { signal });
      map2.setData(C_SRC, fc);
      const n = fc.meta?.count || 0;
      conv.setCount(n);
      const kinds = Object.keys(fc.meta?.inputs?.kinds || {}).filter(k => !['news', 'convergence', 'risk_zone'].includes(k));
      conv.setStatus('ok', n ? null : kinds.length < 3
        ? `None right now. Needs events from at least three kinds of layer; loaded so far: ${kinds.map(k => kindLabel(k).toLowerCase().replace('gps', 'GPS')).join(', ') || 'none'}.`
        : 'None right now: no place has three kinds of event within 24 hours.');
    },
  });
  map2.onClick([C_RING, C_DOT, C_LABEL], (f) => openConvergence(ctx, { kind: 'convergence', id: f.properties.id }));
  map2.onHover([C_RING, C_DOT, C_LABEL], f => f.properties.title);
  ctx.entities.handler('convergence', (e) => openConvergence(ctx, e), { group: 'Intelligence' });
  return { risk, conv };
}

export async function openZone(ctx, entity) {
  const { fmt } = ctx;
  const key = `risk_zone:${entity.id}`;
  const card = ctx.cards.open({ key, kind: 'risk_zone', title: entity.title || 'Risk zone', subtitle: 'Loading…', icon: '▦', accent: INTEL });
  const z = await safeGet(ctx, `/api/intel/risk/${encodeURIComponent(entity.id)}`);
  if (z.__error) {
    card.update({ subtitle: 'Risk zone', sections: [{ text: z.__error.status === 404 ? 'This zone is not active any more. Zones are recomputed every 10 minutes from the last 7 days of events.' : errorText(z.__error) }] });
    return card;
  }
  const name = `${z.short || 'Unnamed'} area`;
  const ent = { kind: 'risk_zone', id: z.id, title: name, subtitle: `${z.level} risk`, lat: z.lat, lon: z.lon, layer: 'intel.risk', bbox: z.bbox };
  card.update({
    title: name, subtitle: [z.country, `${z.country ? String(z.level).toLowerCase() : z.level} risk, last 7 days`].filter(Boolean).join(', '),
    badges: [{ text: `${z.level} risk`, tone: z.level === 'Very high' ? 'red' : z.level === 'High' ? 'amber' : 'grey' }],
    block: [{ label: 'Score', value: fmt.num(z.score, 1) }, { label: 'Events', value: fmt.int(z.events) },
      { label: 'Worst severity', value: (+z.max_severity).toFixed(1) }, { label: 'Area', value: fmt.compact(z.area_km2), unit: 'km²' }],
    sections: [
      { rows: [['Share of local news about violence', z.conflict_share != null ? fmt.pct(z.conflict_share * 100, 0) : null],
        ['Fighting', z.categories?.fighting ? fmt.int(z.categories.fighting) : null], ['Attacks or mass violence', z.categories?.attack ? fmt.int(z.categories.attack) : null],
        ['Assaults', z.categories?.assault ? fmt.int(z.categories.assault) : null], ['Half-degree cells', fmt.int(z.cells)]] },
      { title: 'Places', rows: (z.places || []).slice(0, 6).map(([p, n]) => [short(p, 40), `${n} event${n === 1 ? '' : 's'}`]) },
      { title: 'Contributing events', list: (z.members || []).slice(0, 15).map(e => ({ title: e.title, subtitle: short(e.subtitle, 70),
        detail: (+e.severity).toFixed(1), onClick: () => { ctx.map2.flyTo(e.lat, e.lon, 9); openConflict(ctx, e); } })),
      empty: 'No events listed.' },
      { title: 'How zones work', text: 'Every precisely placed conflict event adds its severity to its half-degree cell, halving every 3.5 days. Each cell is then weighted by how much of all its news is about violence, so busy news hubs count for less. Cells with at least 4 events and a score of 2 are hot; touching hot cells form a zone. Crime reports and province-level places are left out.' },
    ],
    entity: ent, actions: [{ text: 'Show the whole zone', onClick: () => ctx.map2.fitBbox(z.bbox) }],
    foot: `Computed ${z.computed_at ? fmt.ago(z.computed_at) : 'recently'} from GDELT events (machine-coded, not verified).`,
  });
  return card;
}

export async function openConvergence(ctx, entity) {
  const { fmt } = ctx;
  const key = `convergence:${entity.id}`;
  const card = ctx.cards.open({ key, kind: 'convergence', title: entity.title || 'Convergence', subtitle: 'Loading…', icon: '◎', accent: INTEL });
  const c = await safeGet(ctx, `/api/intel/convergences/${encodeURIComponent(entity.id)}`);
  if (c.__error) {
    card.update({ subtitle: 'Convergence', sections: [{ text: errorText(c.__error) }] });
    return card;
  }
  const ent = { kind: 'convergence', id: c.id, title: c.title, subtitle: c.subtitle, lat: c.lat, lon: c.lon, layer: 'intel.convergences', zoom: 8 };
  card.update({
    title: c.title, subtitle: c.subtitle,
    badges: [sevBadge(c.severity), ...c.kinds.map(k => ({ text: `${kindLabel(k.kind)} (${k.count})`, tone: 'grey' }))],
    block: [{ label: 'Kinds', value: String(c.n_kinds) }, { label: 'Events', value: fmt.int(c.n_events) },
      { label: 'Severity', value: (+c.severity).toFixed(1), unit: '/10' }, { label: 'Latest', value: fmt.ago(c.time) }],
    sections: [
      { title: 'The parts', list: (c.members || []).slice(0, 20).map(m => ({ title: m.title || m.kind, subtitle: [kindLabel(m.kind), m.module, m.time ? fmt.ago(m.time) : null].filter(Boolean).join(', '),
        detail: m.severity != null ? (+m.severity).toFixed(1) : '', entity: m.id ? m : null, onClick: m.id ? undefined : () => ctx.map2.flyTo(m.lat, m.lon, 9) })) },
      { title: 'What this means', text: 'Different kinds of event in the same place at the same time. Often coincidence, sometimes linked (for example a strike, a fire and a GPS-jamming cell). Worth a look, not a conclusion.' },
    ],
    entity: ent, actions: [{ text: 'Show the area', onClick: () => ctx.map2.fitBbox(c.bbox) }],
    foot: `Recomputed every 10 minutes; this one ${c.computed_at ? fmt.ago(c.computed_at) : 'recently'}.`,
  });
  return card;
}

export { LEVEL_COLOUR };
