// News: geolocated GDELT reports on the map, the News drawer with theatre feeds, the news card.

import { INTEL, INK, NEWS_RAMP, sevColour, sevBadge, sevLegend, sevTone, bboxParam, safeGet, errorText, short } from './common.js';
import { renderSpikeList } from './spikes.js';

const SRC = 'ov-intel-news';
const L_DOT = 'ov-intel-news-dot';
const DRAWER = 'intel-news';
const NEWS_ICON = 'M4 4.5h9.5v11H5.5A1.5 1.5 0 0 1 4 14V4.5Zm9.5 3H16v6.5a1.5 1.5 0 0 1-1.5 1.5M6.5 7h4.5M6.5 9.5h4.5M6.5 12h3';

export function registerNews(ctx) {
  const { map2, fmt } = ctx;
  const layer = ctx.layers.register({
    id: 'intel.news', group: 'Intelligence', title: 'News', subtitle: 'Geolocated reports, latest per place',
    description: 'The most recent news article GDELT linked to each place. Open the News panel for the monitored theatres.',
    source: 'GDELT 2.0 Events and DOC API (open data, credit the GDELT Project)', color: INTEL, symbol: 'point',
    defaultOn: false, refreshMs: 5 * 60 * 1000, viewDependent: true,
    options: [
      { id: 'hours', label: 'Period', type: 'select', default: '24',
        choices: [{ value: '6', label: 'Last 6 hours' }, { value: '24', label: 'Last 24 hours' }, { value: '72', label: 'Last 3 days' }] },
      { id: 'minSev', label: 'Lowest severity', type: 'range', min: 0, max: 9, step: 1, default: 4, format: v => (v ? `${v}+` : 'any') },
    ],
    legend: sevLegend(NEWS_RAMP),
    enable() {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: L_DOT, type: 'circle', source: SRC, layout: { 'circle-sort-key': ['get', 's'] },
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, ['interpolate', ['linear'], ['get', 's'], 0, 1.4, 10, 3.6],
            8, ['interpolate', ['linear'], ['get', 's'], 0, 3, 10, 7]],
          'circle-color': sevColour('s', NEWS_RAMP), 'circle-opacity': 0.85,
          'circle-stroke-color': INK, 'circle-stroke-width': 0.7, 'circle-pitch-alignment': 'map',
        } }, 'points');
    },
    disable() { map2.removeSource(SRC); },
    async refresh({ bounds, zoom, signal, options }) {
      const fc = await ctx.api.get('/api/intel/news', { bbox: bboxParam(bounds), hours: options.hours, min_severity: options.minSev,
        limit: zoom < 4 ? 2500 : 5000 }, { signal });
      map2.setData(SRC, fc);
      layer.setCount(fc.features.length);
      layer.setStatus('ok', fc.features.length ? null : 'No reports match these settings in this area yet.');
    },
  });
  map2.onClick([L_DOT], (f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    openNews(ctx, { kind: 'news', id: p.id, title: p.l, lat, lon, layer: 'intel.news' });
  });
  map2.onHover([L_DOT], f => `${short(f.properties.l, 70)}, ${f.properties.d}, ${fmt.ago(f.properties.t)}`);
  ctx.entities.handler('news', (e) => openNews(ctx, e), { group: 'Intelligence', icon: '▤' });
  ctx.entities.handler('theatre', (e) => { openDrawer(ctx, { theatre: e.id }); return null; }, { group: 'Intelligence' });
  ctx.ui.toolbarButton({ id: 'intel-news', title: 'News by theatre', label: 'News', icon: NEWS_ICON, order: 40,
    onClick: () => { const d = ctx.ui.getDrawer(DRAWER); if (d) d.close(); else openDrawer(ctx); } });
  ctx.ai.register('intel_show_theatre', ({ id }) => openDrawer(ctx, { theatre: id, fly: true }));
  return layer;
}

export async function openNews(ctx, entity) {
  const { fmt } = ctx;
  const key = `news:${entity.id}`;
  const ent = ctx.dom.validLatLon(entity.lat, entity.lon)
    ? { kind: 'news', id: entity.id, title: entity.title || 'News report', lat: +entity.lat, lon: +entity.lon, layer: 'intel.news' } : null;
  const card = ctx.cards.open({ key, kind: 'news', title: entity.title || 'News report', subtitle: 'Loading…', icon: '▤', accent: INTEL, entity: ent });
  const d = await safeGet(ctx, `/api/intel/news/item/${encodeURIComponent(entity.id)}`);
  if (d.__error) {
    card.update({ subtitle: entity.subtitle || 'News report', sections: [{ text: d.__error.status === 404
      ? 'This report has left the news window (3 days for map points, the current feed for theatre headlines).' : errorText(d.__error) }] });
    return card;
  }
  const located = !(d.approx);
  const entityFull = { kind: 'news', id: d.id, title: d.title, subtitle: [d.domain, d.place].filter(Boolean).join(', '),
    lat: d.lat, lon: d.lon, layer: 'intel.news' };
  const badges = [sevBadge(d.severity)];
  if (d.source === 'gdelt') badges.push({ text: d.headline_from_link ? 'Headline guessed from the link' : 'No headline available', tone: 'grey' });
  else badges.push({ text: 'GDELT DOC headline', tone: 'blue' });
  if (!located) badges.push({ text: 'Placed at the theatre centre', tone: 'amber' });
  const block = d.source === 'gdelt'
    ? [{ label: 'Severity', value: (+d.severity).toFixed(1), unit: '/10' }, { label: 'Mentions', value: fmt.int(d.mentions) },
      { label: 'Tone', value: (+d.tone).toFixed(1) }, { label: 'Reported', value: fmt.ago(d.t) }]
    : [{ label: 'Severity', value: (+d.severity).toFixed(1), unit: '/10' }, { label: 'Outlets', value: fmt.int(d.outlets || 1) },
      { label: 'Seen', value: d.t ? fmt.ago(d.t) : '–' }];
  card.update({
    title: d.title, subtitle: [d.domain, d.place].filter(Boolean).join(', '), badges, block,
    sections: [
      { rows: [['Source', d.domain], ['Place', d.place], ['Country', d.country], ['Event coded', d.action],
        ['Seen', d.t ? fmt.utc(d.t) : null], ['Theatres', (d.theatres || []).filter(Boolean).join(', ') || null],
        ['Language', d.language || null]] },
      d.also?.length ? { title: 'Also reported by', el: ctx.h('ul', { class: 'card-list intel-links' }, d.also.map(a => ctx.h('li', null, ctx.h('a', { href: a.url }, a.domain)))) } : null,
      { title: 'About this item', text: d.source === 'gdelt'
        ? 'One of the articles GDELT read for this place. The headline is rebuilt from the words in the link, so it may be incomplete. Severity is a heuristic from the event type, tone, attention and alarm words.'
        : 'A headline from the GDELT DOC search for this theatre. Its position comes from a place name in the headline, or the theatre centre when none matched.' },
    ],
    links: [{ text: 'Open the article', href: d.url }],
    entity: located ? entityFull : null,
    foot: 'GDELT Project news monitoring. Reports are not verified.',
  });
  return card;
}

// ---------------------------------------------------------------------------------------------
// The News drawer: latest items per theatre, with filters
// ---------------------------------------------------------------------------------------------
let state = { theatre: '', minSev: 0, q: '', hours: 24 };
let timer = null;
let seq = 0;

export function openDrawer(ctx, { theatre = null, fly = false } = {}) {
  const { h } = ctx;
  if (theatre != null) state.theatre = theatre;
  let d = ctx.ui.getDrawer(DRAWER);
  const body = h('div', { class: 'intel-drawer' });
  if (!d) {
    d = ctx.ui.drawer({ id: DRAWER, title: 'News', el: body, width: 460, onClose: () => { clearInterval(timer); timer = null; document.getElementById('tool-intel-news')?.setAttribute('aria-pressed', 'false'); } });
    document.getElementById('tool-intel-news')?.setAttribute('aria-pressed', 'true');
    renderShell(ctx, body);
    timer = setInterval(() => { if (document.visibilityState === 'visible') load(ctx); }, 120000);
  } else {
    const sel = d.body.querySelector('select.intel-theatre');
    if (sel) sel.value = state.theatre;
  }
  load(ctx);
  if (fly && theatre) {
    ctx.api.get('/api/intel/theatres').then((r) => {
      const t = (r.theatres || []).find(x => x.id === theatre);
      if (t) ctx.map2.flyTo(t.lat, t.lon, t.zoom);
    }).catch(() => {});
  }
  return d;
}

function renderShell(ctx, body) {
  const { h, dom } = ctx;
  const theatreSel = h('select', { class: 'intel-theatre', 'aria-label': 'Theatre' },
    h('option', { value: '' }, 'All theatres'));
  ctx.api.get('/api/intel/theatres').then((r) => {
    for (const t of r.theatres || []) theatreSel.append(h('option', { value: t.id, selected: t.id === state.theatre || null }, t.name));
  }).catch(() => {});
  theatreSel.addEventListener('change', () => { state.theatre = theatreSel.value; load(ctx); });
  const hoursSel = h('select', { 'aria-label': 'Period' }, [['6', 'Last 6 hours'], ['24', 'Last 24 hours'], ['72', 'Last 3 days']]
    .map(([v, l]) => h('option', { value: v, selected: String(state.hours) === v || null }, l)));
  hoursSel.addEventListener('change', () => { state.hours = +hoursSel.value; load(ctx); });
  const chips = h('div', { class: 'intel-chips', role: 'group', 'aria-label': 'Lowest severity' });
  for (const [v, l] of [[0, 'All'], [4, '4+'], [6, '6+'], [8, '8+']]) {
    const b = h('button', { type: 'button', class: 'intel-chip', 'aria-pressed': String(state.minSev === v) }, l);
    b.addEventListener('click', () => {
      state.minSev = v;
      for (const c of chips.children) c.setAttribute('aria-pressed', String(c === b));
      load(ctx);
    });
    chips.append(b);
  }
  const q = h('input', { type: 'search', class: 'intel-q', placeholder: 'Filter headlines', 'aria-label': 'Filter headlines', value: state.q });
  q.addEventListener('input', dom.debounce(() => { state.q = q.value.trim(); load(ctx); }, 300));
  body.append(h('div', { class: 'intel-drawer-controls' }, h('div', { class: 'intel-row' }, theatreSel, hoursSel),
    h('div', { class: 'intel-row' }, h('span', { class: 'intel-label' }, 'Severity'), chips, q)),
  h('div', { class: 'intel-drawer-status', role: 'status' }), h('div', { class: 'intel-drawer-list' }, h('p', { class: 'dim intel-pad' }, 'Loading the feeds…')));
}

async function load(ctx) {
  const d = ctx.ui.getDrawer(DRAWER);
  if (!d) return;
  const my = ++seq;
  const list = d.body.querySelector('.intel-drawer-list');
  const status = d.body.querySelector('.intel-drawer-status');
  const res = await safeGet(ctx, '/api/intel/news/feeds', { theatre: state.theatre || null, min_severity: state.minSev,
    q: state.q || null, hours: state.hours, limit: state.theatre ? 60 : 12 });
  if (my !== seq || !ctx.ui.getDrawer(DRAWER)) return;
  const { h, fmt, dom } = ctx;
  dom.clear(list);
  dom.clear(status);
  if (res.__error) {
    list.append(h('p', { class: 'empty intel-pad' }, `The news feed is unavailable: ${errorText(res.__error)}`));
    return;
  }
  const doc = res.doc || {};
  const why = { rate_limited: 'GDELT is rate limiting this network', unavailable: 'GDELT DOC is not answering', bad_response: 'GDELT DOC sent an unexpected reply' }[doc.reason];
  const retry = doc.backoff_s ? ` Next try in about ${Math.max(1, Math.round(doc.backoff_s / 60))} min.` : '';
  status.append(why
    ? h('span', null, h('span', { class: 'intel-dot amber' }), `Headline search is paused: ${why}.${retry} Showing geolocated reports; their headlines are rebuilt from the links.`)
    : h('span', null, h('span', { class: 'intel-dot green' }), `Updated ${fmt.clock(res.updated)} UTC. ${doc.fetched ? 'Headlines from GDELT DOC where available.' : 'Headline search starts while this panel is open.'}`));
  if (!state.q && !state.theatre) {
    // countries far above their own baseline (news-spike detector), above the theatre feeds
    const spk = h('section', { class: 'intel-th intel-spikes' });
    list.append(spk);
    renderSpikeList(ctx, spk).catch(() => {});
  }
  let total = 0;
  for (const th of res.theatres || []) {
    total += th.items.length;
    const show = h('button', { type: 'button', class: 'text-btn', title: `Show ${th.name} on the map` }, 'Show on map');
    show.addEventListener('click', () => (th.bbox ? ctx.map2.fitBbox(th.bbox) : ctx.map2.flyTo(th.lat, th.lon, th.zoom)));
    const head = h('div', { class: 'intel-th-head' }, h('h3', null, th.name),
      h('span', { class: 'intel-th-count tnum' }, th.count ? `${fmt.int(th.count)} item${th.count === 1 ? '' : 's'}` : 'none'), show);
    const ul = h('ul', { class: 'intel-items' });
    for (const it of th.items) ul.append(itemRow(ctx, it));
    if (!th.items.length) ul.append(h('li', { class: 'intel-none' }, state.q || state.minSev ? 'Nothing matches the filters.' : 'No reports in this period yet.'));
    list.append(h('section', { class: 'intel-th' }, head, ul));
  }
  if (!total && !(res.theatres || []).some(t => t.count)) {
    list.prepend(h('p', { class: 'dim intel-pad' }, 'Nothing yet. GDELT history is fetched in the background at first start; it can take a few minutes.'));
  }
}

function itemRow(ctx, it) {
  const { h, fmt } = ctx;
  const pill = h('span', { class: `intel-sev ${sevTone(it.severity)}`, title: `Severity ${(+it.severity).toFixed(1)}` }, (+it.severity).toFixed(0));
  const open = h('button', { type: 'button', class: 'intel-item-main' }, pill,
    h('span', { class: 'intel-item-text' }, h('span', { class: 'intel-item-title' }, it.title),
      h('span', { class: 'intel-item-meta' }, [it.domain, it.place, it.t ? fmt.ago(it.t) : null, it.approx ? 'theatre centre' : null].filter(Boolean).join(', '))));
  open.addEventListener('click', () => {
    if (!it.approx && ctx.dom.validLatLon(it.lat, it.lon)) ctx.map2.flyTo(it.lat, it.lon, 8);
    openNews(ctx, { kind: 'news', id: it.id, title: it.title, lat: it.approx ? null : it.lat, lon: it.approx ? null : it.lon, layer: 'intel.news' });
  });
  const link = h('a', { href: it.url, class: 'intel-item-link', title: 'Open the article', 'aria-label': `Open the article: ${it.title}` }, 'Open');
  return h('li', { class: 'intel-item' }, open, link);
}
