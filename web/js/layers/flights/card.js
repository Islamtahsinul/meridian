// Aircraft cards (entity kind "aircraft"), the trail overlay, follow mode, optional photos and the
// card for OFAC-listed aircraft found through search (entity kind "sanctioned_aircraft").

import { CLASSES, EMERGENCY_LABELS, GROUP, emergencyText, pretty } from './style.js';
import { find as findRow } from './traffic.js';

const TRAIL_SRC = 'ov-flights-trail';
const TRAIL_LINE = 'ov-flights-trail-line';
const TRAIL_EST = 'ov-flights-trail-est';
const TRAIL_DOTS = 'ov-flights-trail-dots';
const ALT_RAMP = ['interpolate', ['linear'], ['coalesce', ['get', 'alt'], 0],
  0, '#ffb547', 4000, '#f08c5a', 12000, '#e05aa8', 24000, '#b07cf2', 36000, '#6cb6ff', 45000, '#86d6f7'];

let ctx = null;
let trailHex = null;
let followHex = null;
const photos = new Map();      // hex -> {src, credit, link} | null

export function init(c) { ctx = c; }

// ------------------------------------------------------------------ trail
function trailFeatures(t) {
  const feats = [];
  const pts = t.points || [];
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1];
    const b = pts[i];
    if (b[3] - a[3] > 900) continue;                     // a gap in coverage: don't draw across it
    feats.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: [[a[0], a[1]], [b[0], b[1]]] },
      properties: { alt: b[2], est: 0 } });
  }
  if (t.estimated && pts.length) {
    const last = pts[pts.length - 1];
    feats.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: [[last[0], last[1]], t.estimated] },
      properties: { alt: last[2], est: 1 } });
  }
  for (const p of pts.slice(-1)) {
    feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [p[0], p[1]] }, properties: { alt: p[2], dot: 1 } });
  }
  return { type: 'FeatureCollection', features: feats };
}

function ensureTrailLayers() {
  ctx.map2.addSource(TRAIL_SRC, { type: 'geojson', data: ctx.map2.emptyFC() });
  ctx.map2.addLayer({ id: TRAIL_LINE, type: 'line', source: TRAIL_SRC,
    filter: ['all', ['==', ['geometry-type'], 'LineString'], ['==', ['get', 'est'], 0]],
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': ALT_RAMP, 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 1.6, 10, 2.8], 'line-opacity': 0.92 } }, 'lines');
  ctx.map2.addLayer({ id: TRAIL_EST, type: 'line', source: TRAIL_SRC,
    filter: ['all', ['==', ['geometry-type'], 'LineString'], ['==', ['get', 'est'], 1]],
    paint: { 'line-color': '#aab4ca', 'line-width': 1.6, 'line-dasharray': [2, 2], 'line-opacity': 0.9 } }, 'lines');
  ctx.map2.addLayer({ id: TRAIL_DOTS, type: 'circle', source: TRAIL_SRC, filter: ['==', ['geometry-type'], 'Point'],
    paint: { 'circle-radius': 2.5, 'circle-color': ALT_RAMP, 'circle-stroke-color': '#111a2c', 'circle-stroke-width': 1 } }, 'lines');
}

async function loadTrail(hex) {
  const t = await ctx.api.get(`/api/flights/trail/${hex}`);
  if (trailHex !== hex) return t;
  ensureTrailLayers();
  ctx.map2.setData(TRAIL_SRC, trailFeatures(t));
  return t;
}

export async function showTrail(hex) {
  trailHex = hex;
  try {
    const t = await loadTrail(hex);
    if (!t.points?.length) ctx.ui.toast('No trail yet: this aircraft has not been tracked long enough.');
    else if (t.note) ctx.ui.toast(t.note);
    return t;
  } catch (err) {
    ctx.ui.toast('The trail could not be loaded right now.', 'red');
    trailHex = null;
    return null;
  }
}

export function hideTrail() {
  trailHex = null;
  ctx.map2.removeSource(TRAIL_SRC);
}

// ------------------------------------------------------------------ photos (opt-in, planespotters.net)
async function photoFor(hex) {
  if (photos.has(hex)) return photos.get(hex);
  photos.set(hex, null);
  try {
    const res = await fetch(`https://api.planespotters.net/pub/photos/hex/${hex}`,
      { credentials: 'omit', referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(8000) });
    if (!res.ok) return null;
    const data = await res.json();
    const p = (data.photos || [])[0];
    const src = p?.thumbnail_large?.src || p?.thumbnail?.src;
    if (!src || !ctx.dom.safeUrl(src) || !ctx.dom.safeUrl(p.link)) return null;
    const out = { src, link: p.link, credit: `Photo ${String(p.photographer || 'unknown').slice(0, 60)}, planespotters.net` };
    photos.set(hex, out);
    return out;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------ card building
function signed(fmt, n) {
  if (n == null) return null;
  const v = Math.round(n / 10) * 10;
  return v > 0 ? `+${fmt.int(v)}` : fmt.int(v);
}

function routeEl(d) {
  const { h } = ctx;
  const r = d.route;
  const o = r.origin || {};
  const t = r.destination || {};
  const place = (a, cls) => h('div', { class: `flights-ap ${cls}` },
    h('span', { class: 'flights-ap-code' }, a.iata || a.icao || '?'),
    h('span', { class: 'flights-ap-name' }, [a.city || a.name, a.country_iso].filter(Boolean).join(', ')));
  const line = h('div', { class: 'flights-route-line', 'aria-hidden': 'true' }, h('span'));
  const box = h('div', { class: 'flights-route' }, place(o, 'from'), line, place(t, 'to'));
  const extra = [];
  if (r.airline?.name) extra.push(h('p', { class: 'flights-route-airline' }, r.airline.name));
  if (d.route_check === 'doubtful') {
    extra.push(h('p', { class: 'flights-route-note' }, 'This route may not match the flight: the aircraft is far from the direct path.'));
  }
  return h('div', null, box, extra);
}

function lastSeenRows(fmt, ls) {
  if (!ls) return [];
  return [['When', `${fmt.utc(ls.t)} (${fmt.ago(ls.t)})`], ['Where', ls.lat != null ? fmt.latlon(ls.lat, ls.lon, 3) : null],
    ['Altitude', ls.alt_ft != null ? fmt.ft(ls.alt_ft) : null], ['Callsign', ls.callsign], ['Data from', ls.source]];
}

function sanctionsSection(d) {
  const s = d.sanctions;
  if (!s) return null;
  const linked = [...(s.linked_to || [])];
  if (s.linked_individuals) linked.push(`${s.linked_individuals} named ${s.linked_individuals === 1 ? 'individual' : 'individuals'} (not shown)`);
  return {
    title: 'OFAC sanctions listing',
    rows: [['Programme', s.program], ['Listed as', [s.registration, s.model].filter(Boolean).join(', ')],
      ['Operator', s.operator ? pretty(s.operator) : null], ['Linked to', linked.map(pretty).join('; ') || null],
      ['Matched by', s.match], ['Serial number', s.msn], ['Built', s.built]],
  };
}

export function buildSpec(d, { hex }) {
  const { fmt } = ctx;
  const live = !!d.live;
  const est = !!d.estimated;
  const emg = d.emergency;
  const accent = GROUP;   // one accent per group; class, emergency and sanctions show as badges
  const badges = [];
  if (d.class && d.class !== 'unknown') badges.push({ text: CLASSES[d.class]?.label || d.class_label, tone: 'grey' });
  if (emg) badges.push(emg.squawk === false
    ? { text: `Emergency status: ${(EMERGENCY_LABELS[emg.code] || 'unspecified')}`, tone: 'red', title: 'Reported in the ADS-B status field without an emergency squawk; this is sometimes a transponder glitch' }
    : { text: `Emergency, ${emergencyText(emg.code)}`, tone: 'red' });
  if (d.priority) badges.push({ text: d.priority, tone: 'blue' });
  if (d.sanctions) badges.push({ text: 'OFAC-listed', tone: 'red', title: 'On the US Treasury OFAC SDN list' });
  if (est) badges.push({ text: 'Estimated position', tone: 'amber', title: 'Dead-reckoned from the last report, not observed' });
  if (!live && !est) badges.push({ text: d.last_seen ? `Not live, last seen ${fmt.ago(d.last_seen.t)}` : 'Not live', tone: 'grey' });
  if (live && d.on_ground) badges.push({ text: 'On the ground', tone: 'grey' });
  if (d.privacy) badges.push({ text: `Privacy programme (${d.privacy})`, tone: 'blue', title: 'The owner asked for limited display' });
  if (live && d.gps_ok === false) badges.push({ text: 'Degraded GPS accuracy', tone: 'amber' });

  const block = (live || est) ? [
    d.on_ground ? { label: 'Altitude', value: 'Ground' } : { label: 'Altitude', value: d.alt_ft != null ? fmt.int(d.alt_ft) : null, unit: 'ft' },
    { label: 'Ground speed', value: d.gs_kt != null ? fmt.int(d.gs_kt) : null, unit: 'kn' },
    { label: 'Track', value: d.track != null ? `${Math.round(d.track) % 360}` : null, unit: d.track != null ? '°' : '' },
    { label: 'Vertical rate', value: d.on_ground ? '0' : signed(fmt, d.vr_fpm), unit: 'ft/min' },
  ] : null;

  const sections = [];
  if (est && d.estimate) {
    sections.push({ title: 'Estimated position', text: `Dead-reckoned from the last report ${fmt.ago(d.pos_t)}`
      + `${d.estimate.gs_kt != null ? ` at ${fmt.int(d.estimate.gs_kt)} kn` : ''}${d.estimate.track != null ? ` on ${d.estimate.track}°` : ''}.`
      + ' This is an estimate while the aircraft is out of receiver coverage, not an observed position.' });
  }
  if (!live && !est) {
    sections.push(d.last_seen
      ? { title: 'Last seen', rows: lastSeenRows(fmt, d.last_seen) }
      : { title: 'Not live', text: 'This aircraft is not in the live feeds right now and has not been seen recently. It may be on the ground with its transponder off or outside ADS-B coverage.' });
  }
  if (d.route) sections.push({ title: 'Route, unverified', el: routeEl(d) });
  else if (d.route_status === 'pending') sections.push({ title: 'Route, unverified', text: 'Looking up the route…' });
  const sanc = sanctionsSection(d);
  if (sanc) sections.push(sanc);
  if (live || est) {
    sections.push({ title: 'Flight', rows: [
      ['Callsign', d.callsign],
      ['Squawk', d.squawk ? `${d.squawk}${d.squawk_label ? `, ${d.squawk_label.toLowerCase()}` : ''}` : null],
      ['Category', d.category ? `${d.category}, ${(d.category_label || '').toLowerCase()}` : null],
      ['Position', d.lat != null ? fmt.latlon(d.lat, d.lon, 4) : null],
      ['Position source', d.position_source],
      ['Navigation accuracy', d.nac_p != null || d.nic != null ? [d.nac_p != null ? `NACp ${d.nac_p}` : null, d.nic != null ? `NIC ${d.nic}` : null].filter(Boolean).join(', ') : null],
      ['Geometric altitude', d.alt_geom_ft != null && !d.on_ground ? fmt.ft(d.alt_geom_ft) : null],
      ['Mode S code', hex.toUpperCase()],
    ] });
  }
  const op = d.operator;
  sections.push({ title: 'Aircraft', rows: [
    ['Registration', d.registration],
    ['Type', d.type],
    ['Model', pretty(d.type_name || d.description)],
    ['Manufacturer', pretty(d.manufacturer)],
    ['Operator', op ? (op.kind === 'private' || op.kind === 'withheld' ? op.text : pretty(op.text)) : null],
    ['Registered in', d.owner_country],
    ['Built', d.year],
    ['Mode S code', !live && !est ? hex.toUpperCase() : null],
  ] });
  if (d.sanctions) {
    sections.push({ text: 'Listed on the US Treasury OFAC Specially Designated Nationals list. Names of private individuals are not shown.' });
  }

  const links = [...(d.links || [])];
  if (d.sanctions?.link) links.push({ text: 'OFAC sanctions search', href: d.sanctions.link });
  const footParts = [];
  if (live || est) footParts.push(`Position from ${d.provider || 'ADS-B'}, ${fmt.ago(d.pos_t)}`);
  if (d.route) footParts.push('route from adsbdb, unverified');
  if (d.provider === 'adsb.lol') footParts.push('adsb.lol data under ODbL');
  const lat = d.lat ?? d.last_seen?.lat;
  const lon = d.lat != null ? d.lon : d.last_seen?.lon;
  const entity = { kind: 'aircraft', id: hex, title: d.title || hex.toUpperCase(), subtitle: pretty(d.subtitle) || 'Aircraft', layer: 'flights', zoom: 10 };
  if (lat != null && lon != null) { entity.lat = lat; entity.lon = lon; }
  return {
    key: `aircraft:${hex}`, kind: 'aircraft', title: d.title || hex.toUpperCase(), subtitle: pretty(d.subtitle) || 'Aircraft',
    icon: '✈', accent, badges, block, sections, links, entity,
    foot: footParts.length ? `${footParts.join(', ')}.` : 'Live data from adsb.fi, adsb.lol and OpenSky.',
  };
}

function seedSpec(hex, seed) {
  const row = findRow(hex);
  const title = row?.cs || row?.reg || seed?.title || hex.toUpperCase();
  return {
    key: `aircraft:${hex}`, kind: 'aircraft', title, subtitle: seed?.subtitle || [row?.typ, row?.reg].filter(Boolean).join(', ') || 'Aircraft',
    icon: '✈', accent: GROUP,
    sections: [{ text: 'Loading the latest details…' }],
    entity: row ? { kind: 'aircraft', id: hex, title, lat: row.lat, lon: row.lon, layer: 'flights' } : null,
  };
}

function actionsFor(hex, d, card) {
  const acts = [];
  acts.push({ text: trailHex === hex ? 'Hide trail' : 'Show trail', primary: false, onClick: async (c) => {
    if (trailHex === hex) hideTrail(); else await showTrail(hex);
    c.update({ actions: actionsFor(hex, d, c) });
  } });
  if (d.live || d.estimated) {
    acts.push({ text: followHex === hex ? 'Stop following' : 'Follow', onClick: (c) => {
      followHex = followHex === hex ? null : hex;
      if (followHex && d.lat != null) ctx.map2.flyTo(d.lat, d.lon, Math.max(ctx.map.getZoom(), 8));
      c.update({ actions: actionsFor(hex, d, c) });
    } });
  }
  return acts;
}

async function withPhoto(spec, hex, card) {
  if (!ctx.settings.get('flights.photos', false)) return;
  const p = await photoFor(hex);
  if (!p || !card.isOpen()) return;
  card.update({ media: { type: 'image', src: p.src, credit: p.credit, alt: 'Aircraft photo' },
    links: [...(card.spec.links || []).filter(l => l.text !== 'Photo on planespotters.net'), { text: 'Photo on planespotters.net', href: p.link }] });
}

export async function openAircraft(hex, seed = null) {
  hex = String(hex || '').toLowerCase();
  if (!/^[0-9a-f]{6}$/.test(hex)) return null;
  const card = ctx.cards.open({
    ...seedSpec(hex, seed),
    live: {
      everyMs: 10000,
      update: async (c) => {
        const d = await ctx.api.get(`/api/flights/aircraft/${hex}`);
        const spec = buildSpec(d, { hex });
        spec.actions = actionsFor(hex, d, c);
        if (c.spec.media) spec.media = c.spec.media;
        if (c.spec.links?.some(l => l.text === 'Photo on planespotters.net')) spec.links = [...spec.links, ...c.spec.links.filter(l => l.text === 'Photo on planespotters.net')];
        if (trailHex === hex) loadTrail(hex).catch(() => {});
        if (followHex === hex && d.lat != null && (d.live || d.estimated)) {
          if (ctx.map2.reducedMotion()) ctx.map.jumpTo({ center: [d.lon, d.lat] });
          else ctx.map.easeTo({ center: [d.lon, d.lat], duration: 900 });
        }
        return spec;
      },
    },
    onClose: () => {
      if (trailHex === hex) hideTrail();
      if (followHex === hex) followHex = null;
    },
  });
  try {
    const d = await ctx.api.get(`/api/flights/aircraft/${hex}`);
    if (!card.isOpen()) return card;
    const spec = buildSpec(d, { hex });
    spec.actions = actionsFor(hex, d, card);
    card.update(spec);
    if (!seed?.lat && d.lat != null && (d.live || d.estimated)) ctx.map2.flyTo(d.lat, d.lon, Math.max(ctx.map.getZoom(), 8));
    withPhoto(spec, hex, card);
  } catch (err) {
    if (card.isOpen()) card.update({ sections: [{ text: err?.status === 400 ? 'That is not a valid aircraft id.' : 'Details are unavailable right now. The card will retry.' }] });
  }
  return card;
}

// ------------------------------------------------------------------ OFAC-listed aircraft from search
export async function openSanctioned(entity) {
  const uid = String(entity.id || '');
  if (!/^\d{1,12}$/.test(uid)) return null;
  let res;
  try { res = await ctx.api.get(`/api/flights/sanctioned/${uid}`); } catch (err) {
    return ctx.cards.open({ key: `sanctioned_aircraft:${uid}`, kind: 'sanctioned_aircraft', title: entity.title || 'Sanctioned aircraft',
      icon: '✈', accent: GROUP, badges: [{ text: 'OFAC-listed', tone: 'red' }], sections: [{ text: 'The sanctions record could not be loaded right now.' }] });
  }
  if (res.live) {
    ctx.map2.flyTo(res.live.lat, res.live.lon, 8);
    return openAircraft(res.live.id, res.live);
  }
  const r = res.record || {};
  const { fmt } = ctx;
  const linked = [...(r.linked_to || [])];
  if (r.linked_individuals) linked.push(`${r.linked_individuals} named ${r.linked_individuals === 1 ? 'individual' : 'individuals'} (not shown)`);
  const modeS = r.mode_s || res.last_seen?.hex || (res.hexes || [])[0];   // listed, or learned from sightings
  const sections = [{ rows: [['Programmes', r.program], ['Model', r.model], ['Operator', r.operator ? pretty(r.operator) : null],
    ['Linked to', linked.map(pretty).join('; ') || null], ['Mode S code', modeS ? String(modeS).toUpperCase() : null],
    ['Serial number', r.msn], ['Built', r.built]] }];
  if (res.last_seen) sections.push({ title: 'Last seen by Meridian', rows: lastSeenRows(fmt, res.last_seen) });
  else sections.push({ text: 'Not seen in the live feeds recently. Many listed aircraft are parked or fly without ADS-B.' });
  sections.push({ text: 'Listed on the US Treasury OFAC Specially Designated Nationals list. Names of private individuals are not shown.' });
  const ent = res.last_seen ? { kind: 'aircraft', id: res.last_seen.hex, title: r.registration || r.name, lat: res.last_seen.lat, lon: res.last_seen.lon, layer: 'flights' } : null;
  return ctx.cards.open({ key: `sanctioned_aircraft:${uid}`, kind: 'sanctioned_aircraft', title: r.registration || r.name || 'Sanctioned aircraft',
    subtitle: ['OFAC-listed aircraft', r.model].filter(Boolean).join(', '), icon: '✈', accent: GROUP,
    badges: [{ text: 'OFAC-listed', tone: 'red' }, { text: res.live ? 'Live' : 'Not live', tone: res.live ? 'green' : 'grey' }],
    block: [
      { label: 'Registration', value: r.registration || '–' },
      modeS ? { label: 'Mode S code', value: String(modeS).toUpperCase() } : null,
      r.built ? { label: 'Built', value: String(r.built) } : null,
      { label: 'Programmes', value: fmt.int((r.programs || []).length || (r.program ? r.program.split(',').length : 0)) },
    ].filter(Boolean),
    sections, links: [{ text: 'OFAC sanctions search', href: r.link || 'https://sanctionssearch.ofac.treas.gov/' }],
    actions: ent ? [{ text: 'Open aircraft', onClick: () => openAircraft(ent.id, ent) }] : [], entity: ent });
}

export function currentTrail() { return trailHex; }
export function following() { return followHex; }
