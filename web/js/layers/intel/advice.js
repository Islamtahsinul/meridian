// Travel advice: official UK FCDO and US State Department levels per country on a common 1-4 scale
// (choropleth through feature-state), the card for a change event, and the country-card section.
// The original wording always sits next to the normalised level.

import { INTEL, ADVICE_RAMP, ADVICE_LEVELS, adviceChip, safeGet, errorText, short } from './common.js';

const SRC = 'ov-intel-advice';
const L_FILL = 'ov-intel-advice-fill';
const L_LINE = 'ov-intel-advice-line';
const KEYS = { fcdo: 'f', us: 'u', max: 'm' };
const SOURCE_NAME = { fcdo: 'UK FCDO', us: 'US State Department' };

export function registerAdvice(ctx) {
  const { map2, fmt } = ctx;
  let last = null;
  let hovered = null;

  const fill = (key) => ['match', ['coalesce', ['feature-state', key], 0], 1, ADVICE_RAMP[0], 2, ADVICE_RAMP[1], 3, ADVICE_RAMP[2],
    4, ADVICE_RAMP[3], 'rgba(0,0,0,0)'];
  // Normal-precaution countries recede: opacity rises with the level (CONTRACT 11: area fills at most 0.35).
  const opacity = (key) => ['match', ['coalesce', ['feature-state', key], 0], 1, 0.05, 2, 0.15, 3, 0.25, 4, 0.34, 0];

  function apply(data) {
    last = data;
    if (!ctx.map.getSource(SRC)) return;
    const isos = new Set([...Object.keys(data.fcdo || {}), ...Object.keys(data.us || {})]);
    for (const iso of isos) {
      const f = data.fcdo?.[iso]?.l || 0;
      const u = data.us?.[iso]?.l || 0;
      ctx.map.setFeatureState({ source: SRC, id: iso }, { f, u, m: Math.max(f, u) });
    }
  }

  const layer = ctx.layers.register({
    id: 'intel.advice', group: 'Intelligence', title: 'Travel advice', subtitle: 'UK FCDO and US State Department levels',
    description: 'Where the UK and US governments advise against travel, on a common scale from 1 (normal precautions) to 4 (do not travel). Click a country for the original wording.',
    source: 'UK FCDO (GOV.UK, Open Government Licence v3.0), US State Department (public domain)', color: ADVICE_RAMP[2], symbol: 'area',
    defaultOn: false, refreshMs: 30 * 60 * 1000,
    options: [{ id: 'by', label: 'Colour by', type: 'select', default: 'fcdo', refresh: false,
      choices: [{ value: 'fcdo', label: 'UK FCDO' }, { value: 'us', label: 'US State Department' }, { value: 'max', label: 'The stricter of the two' }] }],
    legend: [1, 2, 3, 4].map(l => ({ color: ADVICE_RAMP[l - 1], label: `${l}, ${ADVICE_LEVELS[l].toLowerCase()}`, shape: 'area' })),
    async enable({ options }) {
      const shapes = await ctx.api.get('/api/intel/countries/shapes');
      map2.addSource(SRC, { type: 'geojson', data: shapes, promoteId: 'iso2' });
      const key = KEYS[options.by] || 'f';
      map2.addLayer({ id: L_FILL, type: 'fill', source: SRC, paint: { 'fill-color': fill(key), 'fill-opacity': opacity(key) } }, 'polygons');
      map2.addLayer({ id: L_LINE, type: 'line', source: SRC, paint: {
        'line-color': ['case', ['boolean', ['feature-state', 'hover'], false], '#ebe6d6', '#3a5080'],
        'line-width': ['case', ['boolean', ['feature-state', 'hover'], false], 1.6, 0.4], 'line-opacity': 0.8 } }, 'lines');
      if (last) apply(last);
    },
    disable() { map2.removeSource(SRC); hovered = null; },
    async refresh({ signal }) {
      const data = await ctx.api.get('/api/intel/advice', null, { signal });
      apply(data);
      const nf = Object.keys(data.fcdo || {}).length;
      const nu = Object.keys(data.us || {}).length;
      layer.setCount(Math.max(nf, nu));
      const pending = data.pending ? `Reading FCDO country pages: ${fmt.int(data.pending)} to go.` : null;
      if (!nf && !nu) layer.setStatus('warn', pending || 'Travel advice is still being fetched.');
      else layer.setStatus('ok', pending || (data.errors?.fcdo ? `FCDO: ${data.errors.fcdo}` : data.errors?.us ? `US: ${data.errors.us}` : null));
    },
    onOption(id, value) {
      if (id === 'by' && ctx.map.getLayer(L_FILL)) {
        ctx.map.setPaintProperty(L_FILL, 'fill-color', fill(KEYS[value] || 'f'));
        ctx.map.setPaintProperty(L_FILL, 'fill-opacity', opacity(KEYS[value] || 'f'));
      }
    },
  });

  map2.onClick([L_FILL], (f, lngLat) => {
    const iso2 = f.properties.iso2;
    if (iso2) ctx.entities.open({ kind: 'country', id: iso2, title: f.properties.name }, { fly: false });
    else ctx.whatshere.open(lngLat.lat, lngLat.lng);
  });
  map2.onHover([L_FILL], (f) => {
    const iso2 = f.properties.iso2;
    if (hovered !== iso2 && ctx.map.getSource(SRC)) {
      if (hovered) ctx.map.setFeatureState({ source: SRC, id: hovered }, { hover: false });
      if (iso2) ctx.map.setFeatureState({ source: SRC, id: iso2 }, { hover: true });
      hovered = iso2 || null;
    }
    const fc = last?.fcdo?.[iso2]?.l;
    const us = last?.us?.[iso2]?.l;
    const parts = [fc ? `FCDO level ${fc}` : null, us ? `US level ${us}` : null].filter(Boolean);
    return parts.length ? `${f.properties.name}: ${parts.join(', ')}` : f.properties.name;
  });
  ctx.map.on('mouseout', () => {
    if (hovered && ctx.map.getSource(SRC)) ctx.map.setFeatureState({ source: SRC, id: hovered }, { hover: false });
    hovered = null;
  });
  ctx.entities.handler('advice_change', (e) => openAdviceChange(ctx, e), { group: 'Intelligence', icon: '⚐' });
  return layer;
}

/** A level chip: coloured swatch plus "Level 3, reconsider travel". */
function chip(ctx, level) {
  const c = adviceChip(level);
  return ctx.h('span', { class: 'intel-adv-chip' }, ctx.h('span', { class: 'intel-swatch', style: { background: c.colour } }), c.text);
}

/** The "Travel advice" section of the country card (both sources, original wording, latest changes). */
export function adviceSection(ctx, ta) {
  const { h, fmt } = ctx;
  const el = h('div', { class: 'intel-adv' });
  const f = ta?.fcdo;
  const u = ta?.us;
  if (!f && !u) {
    el.append(h('p', { class: 'dim' }, 'No official travel advice on record yet. FCDO pages are read in the background after start-up.'));
    return { title: 'Travel advice', el };
  }
  if (f) {
    const box = h('div', { class: 'intel-adv-src' }, h('div', { class: 'intel-adv-head' }, h('span', { class: 'intel-adv-name' }, 'UK FCDO'), chip(ctx, f.level)),
      h('div', { class: 'intel-adv-label' }, f.label));
    for (const s of (f.sentences || []).slice(0, 2)) box.append(h('p', { class: 'intel-adv-quote' }, short(s, 260)));
    if (f.basis === 'text') box.append(h('p', { class: 'intel-adv-note' }, 'Level read from the advice text: FCDO’s own status code only says parts of the country.'));
    if (f.change) box.append(h('p', { class: 'intel-adv-change' }, `Latest update${f.updated ? ` (${fmt.utc(f.updated).slice(0, 10)})` : ''}: ${short(f.change, 220)}`));
    if (f.url) box.append(h('a', { href: f.url, class: 'intel-adv-link' }, 'FCDO travel advice'));
    el.append(box);
  }
  if (u) {
    const box = h('div', { class: 'intel-adv-src' }, h('div', { class: 'intel-adv-head' }, h('span', { class: 'intel-adv-name' }, 'US State Department'), chip(ctx, u.level)),
      h('div', { class: 'intel-adv-label' }, u.label));
    if (u.summary) box.append(h('p', { class: 'intel-adv-quote' }, short(u.summary, 260)));
    if (u.updated) box.append(h('p', { class: 'intel-adv-change' }, `Issued ${fmt.utc(u.updated).slice(0, 10)}`));
    if (u.url) box.append(h('a', { href: u.url, class: 'intel-adv-link' }, 'US travel advisory'));
    el.append(box);
  }
  const changes = (ta.changes || []).slice(0, 4);
  if (changes.length) {
    const ul = h('ul', { class: 'card-list intel-adv-changes' });
    for (const c of changes) {
      const b = h('button', { type: 'button', class: 'text-btn' }, short(c.text, 140));
      b.addEventListener('click', () => openAdviceChange(ctx, { kind: 'advice_change', id: c.id, title: c.text }));
      ul.append(h('li', null, b, h('span', { class: 'li-s' }, `${SOURCE_NAME[c.source] || c.source}, ${fmt.ago(c.t)}`)));
    }
    el.append(h('div', { class: 'intel-adv-name' }, 'Recent changes'), ul);
  }
  return { title: 'Travel advice', el };
}

export async function openAdviceChange(ctx, entity) {
  const { h, fmt } = ctx;
  const key = `advice_change:${entity.id}`;
  const card = ctx.cards.open({ key, kind: 'advice_change', title: entity.title || 'Travel advice change', subtitle: 'Loading…',
    icon: '⚐', accent: INTEL });
  const d = await safeGet(ctx, `/api/intel/advice/change/${encodeURIComponent(entity.id)}`);
  if (d.__error) {
    card.update({ subtitle: 'Travel advice change', sections: [{ text: errorText(d.__error) }] });
    return card;
  }
  const src = SOURCE_NAME[d.source] || d.source;
  const badges = [{ text: src, tone: 'grey' }];
  if (d.kind === 'areas') badges.push({ text: 'Areas changed', tone: 'blue' });
  else if (d.old_level != null && d.new_level > d.old_level) badges.push({ text: 'Raised', tone: d.new_level >= 4 ? 'red' : 'amber' });
  else if (d.old_level != null) badges.push({ text: 'Lowered', tone: 'green' });
  const cur = d.current || {};
  const now = d.source === 'fcdo' ? cur.fcdo : cur.us;
  const rows = [['Country', d.name], ['Changed', `${fmt.utc(d.t)} (${fmt.ago(d.t)})`],
    ['Before', d.old_level != null ? `${adviceChip(d.old_level).text}${d.data?.old_label ? `: ${d.data.old_label}` : ''}` : null],
    ['After', d.new_level != null ? `${adviceChip(d.new_level).text}${d.data?.new_label ? `: ${d.data.new_label}` : ''}` : null]];
  const sections = [{ rows }];
  if (d.data?.note) sections.push({ title: d.source === 'fcdo' ? 'FCDO’s summary of the change' : 'Summary', text: d.data.note });
  if (now) sections.push({ title: 'Advice now', el: adviceSection(ctx, d.source === 'fcdo' ? { fcdo: now } : { us: now }).el });
  if (d.data?.from_history) sections.push({ text: 'Taken from the change history on the FCDO page the first time Meridian read it.' });
  card.update({
    title: d.text, subtitle: `${src}, ${fmt.utc(d.t).slice(0, 10)}`, icon: d.flag || '⚐', badges,
    block: [{ label: 'Level now', value: d.new_level != null ? String(d.new_level) : '–', unit: '/4' },
      { label: 'Before', value: d.old_level != null ? String(d.old_level) : '–', unit: d.old_level != null ? '/4' : '' },
      { label: 'Changed', value: fmt.ago(d.t) }],
    sections,
    links: [d.data?.url ? { text: d.source === 'fcdo' ? 'FCDO travel advice' : 'US travel advisory', href: d.data.url } : null].filter(Boolean),
    actions: [{ text: 'Country profile', onClick: () => ctx.entities.open({ kind: 'country', id: d.iso2, title: d.name }, { fly: false }) }],
    entity: d.entity || null,
    foot: d.source === 'fcdo' ? 'GOV.UK foreign travel advice, Open Government Licence v3.0.' : 'US State Department travel advisories (US Government work, public domain).',
  });
  return card;
}
