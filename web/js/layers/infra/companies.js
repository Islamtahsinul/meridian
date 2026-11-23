// Listed companies at their headquarters (Wikidata), sized by employees or market value. Cards show
// company facts and a delayed share price when a quote key is configured.

import { COMPANIES, INK } from './shared.js';

const SRC = 'ov-infra-companies';
const L_DOT = 'ov-infra-companies-c';
const L_LBL = 'ov-infra-companies-l';

export function registerCompanies(ctx, s) {
  const { h, fmt, map2, map } = ctx;

  // Size by the chosen figure; companies without it get a small size from their importance score.
  const w = ['coalesce', ['get', 'w'], 0];
  const sizeExpr = (mode) => (mode === 'mcap'
    ? ['case', ['has', 'm'], ['sqrt', ['max', 0, ['get', 'm']]], ['*', 5, w]]
    : ['case', ['has', 'e'], ['/', ['sqrt', ['max', 0, ['get', 'e']]], 30], ['*', 5, w]]);
  const radius = (mode) => {
    const v = sizeExpr(mode);
    return ['interpolate', ['linear'], ['zoom'], 2, ['min', 13, ['+', 1.3, ['*', 0.24, v]]], 6, ['min', 18, ['+', 2, ['*', 0.38, v]]],
      10, ['min', 24, ['+', 3, ['*', 0.55, v]]]];
  };
  // Thin by importance (employees, market value, revenue, an English article) until zoom 6.
  const zoomFilter = ['step', ['zoom'], ['>=', w, 0.6], 3, ['>=', w, 0.45], 4.5, ['>=', w, 0.36], 6, true];
  const labelFilter = ['step', ['zoom'], ['>=', w, 0.82], 6.5, ['>=', w, 0.62], 8.5, ['>=', w, 0.4], 11, true];

  async function show() {
    const mode = layer.options().size;
    map2.addSource(SRC, { type: 'geojson', data: s.geoUrl('companies') });
    map2.addLayer({ id: L_DOT, type: 'circle', source: SRC, filter: zoomFilter, layout: { 'circle-sort-key': ['-', 0, sizeExpr(mode)] }, paint: {
      'circle-radius': radius(mode), 'circle-color': COMPANIES, 'circle-opacity': 0.72,
      'circle-stroke-color': INK, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 2, 0.5, 8, 1] } }, 'points');
    map2.addLayer({ id: L_LBL, type: 'symbol', source: SRC, minzoom: 4, filter: labelFilter, layout: {
      'text-field': ['get', 'n'], 'text-font': ['Noto Sans Regular'], 'text-size': 11, 'text-offset': [0, 0.9], 'text-anchor': 'top',
      'text-max-width': 9, 'text-optional': true, 'symbol-sort-key': ['-', 0, sizeExpr(mode)] },
      paint: { 'text-color': '#d5dcff', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
    const st = await s.status();
    layer.setCount(st.counts?.companies ?? null);
  }

  const layer = ctx.layers.register({
    id: 'infra.companies', group: 'Companies', title: 'Listed companies', subtitle: 'Headquarters from Wikidata',
    description: 'Stock-market listed companies at their headquarters. The biggest show first; zoom in for more. Many headquarters share a city centre position.',
    source: 'Wikidata (CC0); share prices from Finnhub or Twelve Data with a key', color: COMPANIES, symbol: 'point',
    options: [{ id: 'size', label: 'Size by', type: 'select', default: 'employees', choices: [
      { value: 'employees', label: 'Employees' }, { value: 'mcap', label: 'Market value' }] }],
    onOption(id, value) {
      if (id !== 'size' || !map.getLayer(L_DOT)) return;
      map.setPaintProperty(L_DOT, 'circle-radius', radius(value));
      map.setLayoutProperty(L_DOT, 'circle-sort-key', ['-', 0, sizeExpr(value)]);
    },
    legend: [{ color: COMPANIES, label: 'Company headquarters' }],
    async enable() { if (await s.ensureReady('companies', layer, show)) await show(); },
    disable() { map2.removeSource(SRC); },
  });

  map2.onClick([L_DOT], (f) => {
    const [lon, lat] = f.geometry.coordinates;
    openCompany({ kind: 'company', id: f.properties.id, title: f.properties.n, lat, lon, layer: 'infra.companies' });
  });
  map2.onHover([L_DOT], (f) => [f.properties.n, f.properties.t].filter(Boolean).join(', '));

  function money(v) {
    if (!v) return null;
    return s.bigMoney(v.amount, v.currency);
  }

  function quoteEl(q) {
    if (q === undefined) return null;
    if (q === null) return h('p', { class: 'dim' }, 'Fetching a delayed price…');
    if (!q.available) return h('p', { class: 'dim' }, q.reason || 'No price available.');
    const up = (q.change ?? 0) >= 0;
    const cur = q.currency ? ` ${q.currency}` : '';
    return h('div', { class: 'infra-quote' },
      h('div', { class: 'infra-quote-main' }, h('b', { class: 'tnum' }, `${fmt.num(q.price, 2)}${cur}`),
        q.change_pct != null ? h('span', { class: `tnum ${up ? 'infra-up' : 'infra-down'}` }, `${up ? '+' : ''}${fmt.num(q.change, 2)} (${up ? '+' : ''}${fmt.num(q.change_pct, 2)}%)`) : null),
      h('p', { class: 'faint' }, [q.symbol, q.time ? `${fmt.utc(q.time)}` : null, `Delayed, from ${q.source}`].filter(Boolean).join(', ')));
  }

  function spec(c, q) {
    const listings = c.listings || [];
    const primary = listings.find(x => x.ticker) || listings[0];
    const cap = c.market_cap;
    const capText = cap ? money(cap) : null;
    const rows = listings.map(x => h('tr', null, h('td', null, x.exchange || ''), h('td', { class: 'num' }, x.ticker || '–'),
      h('td', { class: 'dim' }, x.mic || '')));
    const flyEx = primary?.exchange_lat != null ? { text: `Fly to ${primary.exchange}`, onClick: () => map2.flyTo(primary.exchange_lat, primary.exchange_lon, 13) } : null;
    return {
      key: `company:${c.id}`, kind: 'company', title: c.name,
      subtitle: s.placeLine(primary ? `${primary.ticker || 'Listed'} on ${primary.exchange}` : 'Listed company', c.hq, c.country_name),
      icon: '¤', accent: COMPANIES,
      badges: (c.industries || []).slice(0, 3).map(t => ({ text: `${String(t).charAt(0).toUpperCase()}${String(t).slice(1)}`, tone: 'grey' })),
      block: [
        c.employees != null ? { label: `Employees${c.employees_year ? ` ${c.employees_year}` : ''}`, value: fmt.compact(c.employees) } : null,
        capText ? { label: `Market value${cap.year ? ` ${cap.year}` : ''}`, ...s.moneyParts(cap.amount, cap.currency) } : null,
        c.revenue ? { label: `Revenue${c.revenue.year ? ` ${c.revenue.year}` : ''}`, ...s.moneyParts(c.revenue.amount, c.revenue.currency) } : null,
        c.founded ? { label: 'Founded', value: String(c.founded) } : null,
      ].filter(Boolean).slice(0, 4),
      sections: [
        listings.some(x => x.quote_symbol) ? { title: 'Share price', el: quoteEl(q) } : null,
        rows.length ? { title: listings.length > 1 ? `Listings (${listings.length})` : 'Listing', el: h('table', { class: 'infra-tbl' }, rows) } : null,
        { title: 'Company', rows: [['Headquarters', c.hq ? `${c.hq}${c.hq_precise ? '' : ' (city position)'}` : null], ['Country', c.country_name],
          ['Industry', (c.industries || []).join(', ')], ['ISIN', (c.isins || []).join(', ')],
          ['Market value in USD', cap?.usd && cap.currency !== 'USD' ? `about ${s.bigMoney(cap.usd, 'USD')}` : null],
          ['Website', c.website ? h('a', { href: c.website }, s.shortUrl(c.website)) : null]] },
        { title: 'Nearby', nearby: { lat: c.lat, lon: c.lon, radius_km: 10, exclude: { kind: 'company', id: c.id } } },
      ],
      links: [c.wikipedia ? { text: 'Wikipedia', href: c.wikipedia } : null, { text: 'Wikidata', href: c.wikidata }].filter(Boolean),
      actions: [flyEx].filter(Boolean),
      entity: { ...c.entity, layer: 'infra.companies' },
      foot: 'Company facts from Wikidata (CC0), as entered there. Share prices are delayed and not for trading.',
    };
  }

  async function loadQuote(c) {
    const sym = (c.listings || []).map(x => x.quote_symbol).find(Boolean);
    if (!sym) return undefined;
    try { return await ctx.api.get('/api/infra/quote', { symbol: sym }); } catch { return { available: false, reason: 'The price service is not answering.' }; }
  }

  async function openCompany(entity) {
    let c;
    try { c = await s.get(s.entityPath('company', entity.id)); } catch (err) { return s.missingCard(entity, 'company', COMPANIES, err); }
    let q = (c.listings || []).some(x => x.quote_symbol) ? null : undefined;
    const card = ctx.cards.open({ ...spec(c, q), live: q === undefined ? null : { everyMs: 60000, update: async () => {
      if (q && !q.available) return null;
      q = await loadQuote(c);
      return spec(c, q);
    } } });
    if (q === null) loadQuote(c).then((res) => { q = res; if (card.isOpen()) card.update(spec(c, q)); });
    return card;
  }

  ctx.entities.handler('company', openCompany, { group: 'Companies' });
  return { openCompany };
}
