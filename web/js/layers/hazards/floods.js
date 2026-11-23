// England flood warnings (Environment Agency): warning and alert areas filled by severity, with
// river and sea gauges near them coloured by how their level compares with the normal range.
// Cards for a warning area and for a gauge (with a two-day level chart), and a "What's here"
// section for warnings near a point.

import { GROUP, INK, EA_LEVEL, EA_TONE, GAUGE, imageryDates, clip } from './kit.js';
import { cap, dayLabel } from './cards.js';

const SRC = 'ov-hazards-floods';
const AREA_IDS = ['ov-hazards-floods-fill', 'ov-hazards-floods-dot'];
const GAUGE_IDS = ['ov-hazards-floods-gauge'];
const LEVEL_WORD = { 1: 'Severe', 2: 'Warning', 3: 'Alert', 4: 'Ended' };
const GAUGE_LABEL = { highest: 'At or above the highest level of recent years', high: 'Above the normal range',
  normal: 'Within the normal range', low: 'Below the normal range', unknown: 'No normal range published' };
const GAUGE_SHORT = { highest: 'Highest of recent years', high: 'Above normal', normal: 'Normal', low: 'Below normal', unknown: 'No normal range' };
const GAUGE_TONE = { highest: 'red', high: 'amber', normal: 'blue', low: 'grey', unknown: 'grey' };
const UNIT_TEXT = { mASD: 'metres above the gauge datum', mAOD: 'metres above Ordnance Datum (about mean sea level)', m: 'metres' };
const ENGLAND = [-6.5, 49.8, 2.1, 55.9];

export function registerFloods(ctx, kit, extras) {
  const { map2, fmt, h } = ctx;
  const lvlColour = ['match', ['get', 'lvl'], 1, EA_LEVEL[1], 2, EA_LEVEL[2], 3, EA_LEVEL[3], EA_LEVEL[4]];
  const gaugeColour = ['match', ['get', 'st'], ...Object.entries(GAUGE).flat(), GAUGE.unknown];
  let loaded = null;

  function addLayers() {
    map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: 'ov-hazards-floods-fill', type: 'fill', source: SRC, filter: ['==', ['get', 'role'], 'area'],
      layout: { 'fill-sort-key': ['-', 5, ['get', 'lvl']] },
      paint: { 'fill-color': lvlColour, 'fill-opacity': ['match', ['get', 'lvl'], 1, 0.34, 2, 0.28, 3, 0.17, 0.06] } }, 'polygons');
    map2.addLayer({ id: 'ov-hazards-floods-line', type: 'line', source: SRC, filter: ['all', ['==', ['get', 'role'], 'area'], ['<', ['get', 'lvl'], 4]],
      layout: { 'line-join': 'round', 'line-sort-key': ['-', 5, ['get', 'lvl']] },
      paint: { 'line-color': lvlColour, 'line-width': ['interpolate', ['linear'], ['zoom'], 5, ['match', ['get', 'lvl'], 3, 0.6, 1.1], 11, ['match', ['get', 'lvl'], 3, 1.2, 2.2]],
        'line-opacity': 0.9 } }, 'lines');
    map2.addLayer({ id: 'ov-hazards-floods-line-ended', type: 'line', source: SRC, filter: ['all', ['==', ['get', 'role'], 'area'], ['==', ['get', 'lvl'], 4]],
      paint: { 'line-color': EA_LEVEL[4], 'line-width': 1, 'line-opacity': 0.7, 'line-dasharray': [2, 2] } }, 'lines');
    // at country scale the areas are small: a dot keeps each one visible
    map2.addLayer({ id: 'ov-hazards-floods-dot', type: 'circle', source: SRC, maxzoom: 8,
      filter: ['all', ['==', ['get', 'role'], 'label'], ['<', ['get', 'lvl'], 4]],
      layout: { 'circle-sort-key': ['-', 5, ['get', 'lvl']] },
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, ['match', ['get', 'lvl'], 3, 3, 4.5], 7, ['match', ['get', 'lvl'], 3, 4.5, 6]],
        'circle-color': lvlColour, 'circle-stroke-color': INK, 'circle-stroke-width': 1, 'circle-opacity': 0.95, 'circle-pitch-alignment': 'map' } }, 'points');
    map2.addLayer({ id: 'ov-hazards-floods-gauge', type: 'circle', source: SRC, minzoom: 7, filter: ['==', ['get', 'role'], 'gauge'],
      layout: { 'circle-sort-key': ['get', 'rank'] },
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, 3.4, 12, 6.5], 'circle-color': gaugeColour,
        'circle-stroke-color': INK, 'circle-stroke-width': 1.2, 'circle-opacity': ['case', ['==', ['get', 'old'], 1], 0.5, 1],
        'circle-pitch-alignment': 'map' } }, 'points');
    map2.addLayer({ id: 'ov-hazards-floods-gauge-label', type: 'symbol', source: SRC, minzoom: 11, filter: ['==', ['get', 'role'], 'gauge'],
      layout: { 'text-field': ['concat', ['to-string', ['get', 'v']], ' m'], 'text-font': ['Open Sans Regular'], 'text-size': 10,
        'text-anchor': 'left', 'text-offset': [0.9, 0], 'text-optional': true },
      paint: { 'text-color': '#ebe6d6', 'text-halo-color': INK, 'text-halo-width': 1.2 } }, 'top');
    map2.addLayer({ id: 'ov-hazards-floods-label', type: 'symbol', source: SRC, minzoom: 8, filter: ['all', ['==', ['get', 'role'], 'label'], ['<', ['get', 'lvl'], 4]],
      layout: { 'text-field': ['get', 'name'], 'text-font': ['Open Sans Regular'], 'text-size': 10.5, 'text-max-width': 10,
        'text-optional': true, 'symbol-sort-key': ['get', 'lvl'] },
      paint: { 'text-color': '#ebe6d6', 'text-halo-color': INK, 'text-halo-width': 1.3 } }, 'top');
  }

  const layer = ctx.layers.register({
    id: 'hazards.floods', group: GROUP, title: 'Flood warnings (England)', subtitle: 'Environment Agency',
    description: 'Flood alerts, flood warnings and severe flood warnings from the Environment Agency, with the areas they cover and the river and sea levels near them. England only.',
    source: 'Environment Agency real-time flood-monitoring API (Open Government Licence)', color: EA_LEVEL[2], symbol: 'area',
    defaultOn: false, refreshMs: 300000, viewDependent: true,
    options: [
      { id: 'min', label: 'Show', type: 'select', default: '3', choices: [
        { value: '3', label: 'Alerts and warnings' }, { value: '2', label: 'Warnings and severe warnings' }, { value: '1', label: 'Severe warnings only' }] },
      { id: 'gauges', label: 'River and sea gauges near warnings', type: 'toggle', default: true },
      { id: 'ended', label: 'Warnings no longer in force', type: 'toggle', default: false },
    ],
    onOption() { loaded = null; },
    legend: [{ color: EA_LEVEL[1], label: 'Severe flood warning: danger to life', shape: 'area' },
      { color: EA_LEVEL[2], label: 'Flood warning: flooding expected', shape: 'area' },
      { color: EA_LEVEL[3], label: 'Flood alert: flooding possible', shape: 'area' },
      { color: GAUGE.high, label: 'Gauge above its normal range' }, { color: GAUGE.normal, label: 'Gauge within its normal range' }],
    async enable() { addLayers(); loaded = null; },
    async disable() { map2.removeSource(SRC); loaded = null; },
    async refresh({ bounds, zoom, signal, options, reason }) {
      const level = zoom >= 9 ? 'fine' : 'coarse';
      const key = `${options.min}|${options.gauges}|${options.ended}`;
      const fresh = loaded && Date.now() - loaded.at < 240000 && loaded.key === key;
      if (reason === 'view' && fresh && level === 'coarse' && loaded.level === 'coarse') return;
      if (reason === 'view' && fresh && level === 'fine' && loaded.level === 'fine' && inside(bounds, loaded.bbox)) return;
      const box = level === 'fine' ? expand(bounds) : null;
      const params = { zoom: zoom.toFixed(1), bbox: box ? box.join(',') : null, min: options.min || 3,
        gauges: options.gauges === false ? 0 : 1, ended: options.ended ? 1 : 0 };
      const data = await kit.fetchReady('/api/hazards/floods', params, signal, d => d.meta?.source?.floods?.pending);
      map2.setData(SRC, data);
      loaded = { level, at: Date.now(), key, bbox: box };
      const m = data.meta || {};
      layer.setCount(m.count ?? 0);
      const src = m.source || {};
      if (src.floods?.error && !src.floods?.updated_at) { layer.setStatus('error', `Environment Agency unavailable: ${src.floods.error}`); return; }
      const c = m.counts || {};
      const parts = [c.severe ? `${fmt.int(c.severe)} severe flood warning${c.severe === 1 ? '' : 's'}` : null,
        c.warning ? `${fmt.int(c.warning)} flood warning${c.warning === 1 ? '' : 's'}` : null,
        c.alert ? `${fmt.int(c.alert)} flood alert${c.alert === 1 ? '' : 's'}` : null].filter(Boolean);
      let msg = parts.length ? `${joinWords(parts)} in force in England.` : 'No flood warnings or alerts in force in England.';
      const busy = [];
      if (m.outlines_pending) busy.push(`${fmt.int(m.outlines_pending)} area outline${m.outlines_pending === 1 ? '' : 's'}`);
      if (options.gauges !== false && parts.length) {
        if (!m.stations || !m.readings_at) busy.push('gauge levels');
        else msg += ` ${fmt.int(m.gauges)} gauge${m.gauges === 1 ? '' : 's'} near them${m.gauges_high ? `, ${fmt.int(m.gauges_high)} above normal` : ''}.`;
      }
      if (src.floods?.error) msg += ` The last refresh failed (${src.floods.error}).`;
      if (busy.length) {
        layer.setStatus('warn', `${msg} Still loading ${busy.join(' and ')}.`);
        loaded = null;
        kit.retryLater(layer, 8000);
        return;
      }
      layer.setStatus(src.floods?.error ? 'warn' : 'ok', msg);
    },
  });

  function inside(b, outer) {
    if (!outer) return false;
    return b[0] >= outer[0] && b[1] >= outer[1] && b[2] <= outer[2] && b[3] <= outer[3] && b[0] <= b[2];
  }
  function expand(b) {
    const [w, s, e, n] = b;
    const dx = Math.min(2, (e - w) * 0.25);
    const dy = Math.min(1.5, (n - s) * 0.25);
    return [Math.max(-180, w - dx), Math.max(-85, s - dy), Math.min(180, e + dx), Math.min(85, n + dy)].map(v => +v.toFixed(3));
  }

  map2.onClick([...AREA_IDS, ...GAUGE_IDS], (f, lngLat) => {
    const p = f.properties;
    if (p.role === 'gauge') {
      const [lon, lat] = f.geometry.coordinates;
      ctx.entities.open({ kind: 'gauge', id: p.id, title: p.name, lat, lon, layer: 'hazards.floods' }, { fly: false });
      return;
    }
    const pt = f.geometry.type === 'Point' ? f.geometry.coordinates : [lngLat.lng, lngLat.lat];
    ctx.entities.open({ kind: 'flood', id: p.id, title: p.name, subtitle: p.sev, lat: pt[1], lon: pt[0], layer: 'hazards.floods' }, { fly: false });
  });
  map2.onHover([...AREA_IDS, ...GAUGE_IDS], (f) => {
    const p = f.properties;
    if (p.role === 'gauge') {
      const trend = p.tr ? `, ${p.tr}` : '';
      return `${p.name}${p.river ? ` (${p.river})` : ''}: ${fmt.num(+p.v, 2)} m, ${GAUGE_SHORT[p.st] || 'level'}${trend}${+p.sea ? ', tidal' : ''}`;
    }
    return `${p.sev}: ${p.name}`;
  });

  // ------------------------------------------------------------------ shared card bits
  const gaugeEntity = (g) => ({ kind: 'gauge', id: `eagauge:${g.ref}`, title: g.label, subtitle: gaugeLine(g), lat: g.lat, lon: g.lon, layer: 'hazards.floods' });
  function gaugeLine(g) {
    const where = g.river || (g.kind === 'sea' ? 'Sea level' : g.town || '');
    if (g.value == null) return [where, 'no recent reading'].filter(Boolean).join(', ');
    return [where, `${fmt.num(g.value, 2)} m`, GAUGE_SHORT[g.state]?.toLowerCase()].filter(Boolean).join(', ');
  }
  const pos = (lat, lon) => (ctx.dom.validLatLon(lat, lon) ? fmt.latlon(+lat, +lon, 3) : null);

  // ------------------------------------------------------------------ warning card
  function floodCard(d, entity) {
    const w = d.data;
    const lvl = w.level;
    const gauges = w.gauges || [];
    const withLevel = gauges.filter(g => g.value != null);
    const high = withLevel.filter(g => g.state === 'high' || g.state === 'highest').length;
    const badges = [{ text: w.severity, tone: EA_TONE[lvl] || 'grey' }];
    if (w.tidal) badges.push({ text: 'Tidal', tone: 'blue', title: 'Flooding from the sea or a tidal river' });
    if (high) badges.push({ text: `${high} gauge${high === 1 ? '' : 's'} above normal`, tone: 'amber' });
    const sections = [{ el: h('p', { class: `hazards-meaning hazards-meaning-${lvl}` }, `${w.severity}. ${w.meaning}`) }];
    if (w.message) sections.push({ title: 'Message', el: kit.expandable(w.message, 600) });
    sections.push({ rows: [
      ['Area', w.description || w.name], ['River or sea', w.river_sea], ['County', w.county],
      ['Local authority', w.local_authority && w.local_authority !== w.county ? w.local_authority : null],
      ['Last reviewed', w.raised ? kit.when(w.raised) : null], ['Severity set', w.severity_changed ? kit.when(w.severity_changed) : null],
      ['Message updated', w.message_changed && w.message_changed !== w.severity_changed ? kit.when(w.message_changed) : null],
      ['Floodline', `${w.floodline}${w.quick_dial ? `, quick dial ${w.quick_dial}` : ''}`],
    ] });
    if (withLevel.length) {
      sections.push({ title: `River and sea gauges (${withLevel.length})`, list: withLevel.slice(0, 10).map(g => ({
        title: g.label, subtitle: gaugeLine(g), detail: g.trend ? cap(g.trend) : '', entity: gaugeEntity(g) })) });
    } else if (w.stations_loaded === false || !w.readings_at) {
      sections.push({ title: 'River and sea gauges', text: 'Levels load while the flood warnings layer is on.' });
    }
    const fitTo = w.bbox;
    const lat = w.lat ?? entity.lat;
    const lon = w.lon ?? entity.lon;
    return {
      title: w.name, subtitle: clip([w.river_sea, w.county].filter(Boolean).join(', ') || 'England', 140),
      badges,
      block: [
        { label: 'Severity', value: LEVEL_WORD[lvl] || '–' },
        { label: 'Reviewed, UTC', value: w.raised ? fmt.clock(w.raised).slice(0, 5) : '–', unit: w.raised ? dayLabel(w.raised) : '' },
        { label: 'Gauges near', value: fmt.int(withLevel.length), unit: high ? `${high} high` : '' },
      ],
      sections,
      links: [{ text: 'Check for flooding on GOV.UK', href: w.page }, { text: 'This warning in the API', href: w.api_url }],
      actions: [fitTo ? { text: 'Zoom to the area', onClick: () => ctx.map2.fitBbox(fitTo, { maxZoom: 13 }) } : null,
        ctx.dom.validLatLon(lat, lon) ? kit.compareAction(+lat, +lon, 12, imageryDates(w.severity_changed || w.raised, 10, 3)) : null].filter(Boolean),
      entity: d.entity && ctx.dom.validLatLon(d.entity.lat, d.entity.lon) ? d.entity : (ctx.dom.validLatLon(entity.lat, entity.lon) ? entity : null),
      foot: `${w.credit}. In an emergency call 999.`,
      after: (card) => { if (w.outline) kit.select(card.key, w.outline, EA_LEVEL[lvl] || EA_LEVEL[3]); },
    };
  }

  // ------------------------------------------------------------------ gauge card
  function gaugeCard(d, entity) {
    const g = d.data;
    const v = g.value;
    const badges = [{ text: GAUGE_SHORT[g.state], tone: GAUGE_TONE[g.state] || 'grey', title: GAUGE_LABEL[g.state] }];
    if (g.trend) badges.push({ text: cap(g.trend), tone: 'grey', title: 'Change since the previous reading Meridian saw' });
    if (g.kind === 'sea') badges.push({ text: 'Tidal', tone: 'blue' });
    if (g.stale) badges.push({ text: 'Old reading', tone: 'grey', title: 'No new reading for more than 6 hours' });
    const chartBox = h('div', { class: 'hazards-chart-box' }, h('p', { class: 'dim' }, 'Loading the past two days…'));
    const range = g.typical_low != null && g.typical_high != null ? `${fmt.num(g.typical_low, 2)} to ${fmt.num(g.typical_high, 2)} m` : null;
    const sections = [{ title: 'Past two days', el: chartBox }, { rows: [
      ['Now', v != null ? `${fmt.num(v, 3)} m, ${GAUGE_LABEL[g.state].toLowerCase()}` : 'No recent reading'],
      ['Reading taken', g.time ? kit.when(g.time) : null], ['Normal range', range],
      ['Highest of recent years', g.recent_high?.value != null ? `${fmt.num(g.recent_high.value, 2)} m${onDate(g.recent_high.time)}` : null],
      ['Highest on record', g.record_high?.value != null ? `${fmt.num(g.record_high.value, 2)} m${onDate(g.record_high.time)}` : null],
      ['River', g.river], ['Town', g.town], ['Catchment', g.catchment],
      ['Measures', `${g.qualifier === 'Tidal Level' ? 'Tidal level' : g.qualifier === 'Downstream Stage' ? 'Level downstream of a structure' : 'River level'}, ${UNIT_TEXT[g.unit] || g.unit}`],
      ['Station', g.ref], ['Position', pos(g.lat, g.lon)],
    ] }];
    if (g.warnings?.length) sections.push({ title: 'Warnings around this gauge', list: g.warnings.map(x => ({ title: x.title, subtitle: x.subtitle, entity: x })) });
    return {
      title: g.label, subtitle: [g.river || (g.kind === 'sea' ? 'Sea level' : null), g.town].filter(Boolean).join(', ') || 'River gauge',
      badges,
      block: [
        { label: 'Level', value: v != null ? fmt.num(v, 2) : '–', unit: 'm' },
        { label: 'Top of normal', value: g.typical_high != null ? fmt.num(g.typical_high, 2) : '–', unit: g.typical_high != null ? 'm' : '' },
        { label: 'Recent high', value: g.recent_high?.value != null ? fmt.num(g.recent_high.value, 2) : '–', unit: g.recent_high?.value != null ? 'm' : '' },
        { label: 'Read, UTC', value: g.time ? fmt.clock(g.time).slice(0, 5) : '–', unit: g.time ? dayLabel(g.time) : '' },
      ],
      sections,
      links: [g.page ? { text: 'Check for flooding on GOV.UK', href: g.page } : null].filter(Boolean),
      entity: d.entity && ctx.dom.validLatLon(d.entity.lat, d.entity.lon) ? d.entity : (ctx.dom.validLatLon(entity.lat, entity.lon) ? entity : null),
      foot: `${g.credit}.`,
      after: (card) => fillChart(card, chartBox, `eagauge:${g.ref}`),
    };
  }

  async function fillChart(card, box, id) {
    let r;
    try {
      r = await ctx.api.get('/api/hazards/floods/gauge-history', { id, hours: 48 });
    } catch {
      r = { points: [] };
    }
    if (!card.isOpen()) return;
    ctx.dom.clear(box);
    if (!r.points?.length) { box.append(h('p', { class: 'dim' }, r.note || 'No readings in the past two days.')); return; }
    box.append(chart(r));
  }

  /** A small level chart: the line, the top of the normal range (dashed) and the times at either end. */
  function chart(r) {
    const ns = 'http://www.w3.org/2000/svg';
    const W = 320; const H = 118; const L = 40; const R = 8; const T = 8; const B = 20;
    const pts = r.points;
    const vals = pts.map(p => p.v);
    let lo = Math.min(...vals); let hi = Math.max(...vals);
    const th = r.typical_high;
    if (th != null && th < hi + (hi - lo + 0.2) * 1.5) hi = Math.max(hi, th);
    if (hi - lo < 0.05) { hi += 0.05; lo -= 0.05; }
    const padv = (hi - lo) * 0.08; lo -= padv; hi += padv;
    const t0 = pts[0].t; const t1 = pts[pts.length - 1].t || t0 + 1;
    const x = t => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
    const y = v => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    const el = (tag, attrs, text) => {
      const n = document.createElementNS(ns, tag);
      for (const [k, val] of Object.entries(attrs)) n.setAttribute(k, String(val));
      if (text != null) n.textContent = text;
      return n;
    };
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'hazards-chart', role: 'img', 'aria-label': 'Water level over the past two days' });
    svg.append(el('line', { x1: L, y1: H - B, x2: W - R, y2: H - B, class: 'hazards-chart-axis' }));
    if (th != null && th >= lo && th <= hi) {
      svg.append(el('line', { x1: L, y1: y(th).toFixed(1), x2: W - R, y2: y(th).toFixed(1), class: 'hazards-chart-normal' }));
      svg.append(el('text', { x: W - R, y: (y(th) - 3).toFixed(1), 'text-anchor': 'end', class: 'hazards-chart-note' }, 'top of normal'));
    }
    let d = '';
    pts.forEach((p, i) => { d += `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`; });
    svg.append(el('path', { d, class: 'hazards-chart-line' }));
    svg.append(el('text', { x: L - 4, y: T + 8, 'text-anchor': 'end', class: 'hazards-chart-tick' }, `${fmt.num(hi, 2)} m`));
    svg.append(el('text', { x: L - 4, y: H - B, 'text-anchor': 'end', class: 'hazards-chart-tick' }, `${fmt.num(lo, 2)} m`));
    svg.append(el('text', { x: L, y: H - 5, 'text-anchor': 'start', class: 'hazards-chart-tick' }, `${dayLabel(t0)} ${fmt.clock(t0).slice(0, 5)}`));
    svg.append(el('text', { x: W - R, y: H - 5, 'text-anchor': 'end', class: 'hazards-chart-tick' }, `${dayLabel(t1)} ${fmt.clock(t1).slice(0, 5)} UTC`));
    const last = pts[pts.length - 1];
    svg.append(el('circle', { cx: x(last.t).toFixed(1), cy: y(last.v).toFixed(1), r: 3, class: 'hazards-chart-dot' }));
    return svg;
  }

  extras.builders = extras.builders || {};
  extras.builders.ea_flood = floodCard;
  extras.builders.ea_gauge = gaugeCard;

  // ------------------------------------------------------------------ What's here
  ctx.whatshere.section(async (lat, lon) => {
    if (!(lon >= ENGLAND[0] && lon <= ENGLAND[2] && lat >= ENGLAND[1] && lat <= ENGLAND[3])) return null;
    let r;
    try {
      r = await ctx.api.get('/api/hazards/floods/here', { lat: lat.toFixed(4), lon: lon.toFixed(4) });
    } catch {
      return null;
    }
    const warnings = r.warnings || [];
    const gauges = (r.gauges || []).filter(g => warnings.length || g.state === 'high' || g.state === 'highest');
    if (!warnings.length && !gauges.length) return null;
    const ul = h('ul', { class: 'card-list hazards-here' });
    const add = (glyph, title, sub, entity) => {
      const b = h('button', { type: 'button' }, h('span', { class: 'li-t' }, h('span', { class: 'hazards-glyph', 'aria-hidden': 'true' }, glyph), title),
        h('span', { class: 'li-s' }, sub));
      b.addEventListener('click', () => ctx.entities.open(entity));
      ul.append(h('li', null, b));
    };
    for (const w of warnings) add('≋', w.title, `${w.severity}, ${w.inside ? 'covers this spot' : `${fmt.km(w.distance_km)} away`}`, w);
    for (const g of gauges) add('▮', g.title, `${g.river ? `${g.river}, ` : ''}${fmt.num(g.value, 2)} m, ${(GAUGE_SHORT[g.state] || '').toLowerCase()}, ${fmt.km(g.distance_km)} away`, g);
    const note = warnings.length ? null : h('p', { class: 'dim' }, 'No flood warnings within 5 km, but a gauge nearby is above normal.');
    return { title: 'Flood warnings near here', el: note ? h('div', null, ul, note) : ul };
  });

  return { layer };
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function onDate(t) {
  if (!t) return '';
  const d = new Date(t * 1000);
  return ` on ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

function joinWords(parts) {
  if (parts.length < 2) return parts.join('');
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;
}
