// Markets drawer: stock markets, metals, energy, food, currencies and crypto from keyless public
// sources (and share-price keys when configured). Refreshes every 3 minutes while open.

import { COMPANIES } from './shared.js';

const ICON = 'M3 15.5l4-5 3 3 4-6.5 3 4M3 17h14';
const REFRESH_MS = 180000;

export function registerMarkets(ctx) {
  const { h, fmt, ui } = ctx;
  let drawer = null;
  let timer = null;
  let quick = null;
  let quickTries = 0;
  let btn = null;

  const fixed = (n, d) => new Intl.NumberFormat('en-GB', { minimumFractionDigits: d, maximumFractionDigits: d }).format(n);
  const isFx = (q) => /^[A-Z]{6}$/.test(q.symbol || '');

  function decimals(q) {
    const p = Math.abs(q.price);
    if (isFx(q)) return p >= 20 ? 2 : 4;
    if (p >= 1000) return 0;
    if (p >= 100) return 1;
    return 2;
  }

  function valueText(q) {
    if (!q.available || q.price == null) return '–';
    const n = fixed(q.price, decimals(q));
    return q.currency === 'USD' && !isFx(q) ? `$${n}` : n;
  }

  function meta(q) {
    if (q.pending) return 'Loading…';
    if (!q.available) return q.reason || 'Not available';
    const parts = [];
    if (q.via) parts.push(`via the ${q.via}`);
    if (q.unit && q.unit !== 'index') parts.push(q.unit);
    if (q.delay === 'live') parts.push(q.time ? `live, ${fmt.ago(q.time)}` : 'live');
    else if (q.delay === 'monthly average') parts.push(`${q.as_of} average`);
    else if (q.as_of) parts.push(`${q.delay}, ${q.as_of}`);
    else if (q.time) parts.push(`${q.delay}, ${fmt.utc(q.time)}`);
    else if (q.delay) parts.push(q.delay);
    return parts.join(', ');
  }

  function row(q) {
    const up = (q.change_pct ?? 0) >= 0;
    const chg = q.available && q.change_pct != null
      ? h('span', { class: `infra-mk-chg tnum ${Math.abs(q.change_pct) < 0.005 ? '' : up ? 'infra-up' : 'infra-down'}`,
        title: q.change_basis ? `Change since ${q.change_basis}` : (q.delay === 'monthly average' ? 'Change from the month before' : 'Change from the previous value') },
      `${up ? '+' : ''}${fixed(q.change_pct, 2)}%`) : h('span', { class: 'infra-mk-chg faint' }, '');
    const content = [h('span', { class: 'infra-mk-name' }, q.name), h('span', { class: 'infra-mk-val tnum' }, valueText(q)), chg,
      h('span', { class: `infra-mk-meta${q.available ? '' : ' dim'}` }, meta(q))];
    if (q.fly && ctx.dom.validLatLon(q.fly.lat, q.fly.lon)) {
      const b = h('button', { type: 'button', class: 'infra-mk-row', title: `Fly to ${q.fly.label}` }, content);
      b.addEventListener('click', () => ctx.map2.flyTo(q.fly.lat, q.fly.lon, q.fly.zoom || 13));
      return b;
    }
    return h('div', { class: 'infra-mk-row' }, content);
  }

  // Markets that need a share-price key collapse into one line with links that fly to each exchange.
  function keyNote(items) {
    const links = items.filter(i => i.fly).map((i, n) => {
      const b = h('button', { type: 'button', class: 'text-btn infra-mk-fly', title: `Fly to ${i.fly.label}` }, i.name);
      b.addEventListener('click', () => ctx.map2.flyTo(i.fly.lat, i.fly.lon, i.fly.zoom || 13));
      return n ? [', ', b] : b;
    });
    return h('p', { class: 'infra-mk-note' }, 'With a free FINNHUB_API_KEY or TWELVEDATA_API_KEY in .env these show through US-listed country funds: ', links, '.');
  }

  function render(body, doc) {
    ctx.dom.clear(body);
    const wrap = h('div', { class: 'infra-mk' });
    for (const g of doc.groups || []) {
      const keyed = g.items.filter(i => !i.available && !i.pending && /FINNHUB_API_KEY/.test(i.reason || ''));
      const shown = g.items.filter(i => !keyed.includes(i));
      if (!shown.length && !keyed.length) continue;
      wrap.append(h('section', { class: 'infra-mk-group' }, h('h3', null, g.title),
        shown.length ? h('div', { class: 'infra-mk-list' }, shown.map(row)) : null, keyed.length ? keyNote(keyed) : null));
    }
    const keys = doc.keys || {};
    wrap.append(h('div', { class: 'infra-mk-foot' },
      h('p', null, 'Delayed, daily or monthly reference prices. Not for trading.'),
      h('p', null, 'Metals and crypto: gold-api.com. Currencies: European Central Bank reference rates. Energy, food and ore: World Bank Pink Sheet monthly averages', doc.commodities_updated ? ` (updated ${doc.commodities_updated})` : '', '.'),
      doc.fred_down ? h('p', null, 'FRED, which supplies index levels and daily oil and gas prices, is not answering; monthly averages are shown instead where they exist.') : h('p', null, 'Stock indices, daily oil and US gas: FRED (St. Louis Fed), end of day.'),
      !keys.finnhub && !keys.twelvedata ? h('p', null, 'Share and fund prices need a free FINNHUB_API_KEY or TWELVEDATA_API_KEY in .env.') : null,
      h('p', { class: 'faint' }, doc.updated_at ? `Updated ${fmt.clock(doc.updated_at)} UTC` : 'Updating…')));
    body.append(wrap);
  }

  async function load(body) {
    let doc;
    try { doc = await ctx.api.get('/api/infra/markets'); } catch (err) {
      ctx.dom.clear(body);
      body.append(h('p', { class: 'empty', style: { padding: '14px' } }, 'Market prices are unavailable right now.'));
      return;
    }
    if (!drawer) return;
    render(body, doc);
    const pending = doc.refreshing || !doc.updated_at || (doc.groups || []).some(g => g.items.some(i => i.pending));
    clearTimeout(quick);
    if (pending && quickTries < 8) { quickTries += 1; quick = setTimeout(() => load(body), 3000); }
  }

  function open() {
    if (drawer) { drawer.close(); return; }
    const body = h('div', { class: 'infra-mk-body' }, h('p', { class: 'dim', style: { padding: '14px' } }, 'Loading prices…'));
    const refresh = h('button', { class: 'icon-btn', type: 'button', title: 'Refresh prices', 'aria-label': 'Refresh prices' },
      ctx.dom.svgIcon('M15.5 8.5A6 6 0 1 0 16 11M16 4v4.5h-4.5'));
    refresh.addEventListener('click', () => { quickTries = 0; load(body); });
    drawer = ui.drawer({ id: 'infra-markets', title: 'Markets', el: body, width: 400, tools: refresh, onClose() {
      clearInterval(timer); clearTimeout(quick); timer = null; drawer = null; btn?.setAttribute('aria-pressed', 'false');
    } });
    btn?.setAttribute('aria-pressed', 'true');
    quickTries = 0;
    load(body);
    timer = setInterval(() => { if (document.visibilityState === 'visible') { quickTries = 0; load(body); } }, REFRESH_MS);
  }

  btn = ui.toolbarButton({ id: 'infra-markets', title: 'Markets', label: 'Markets', icon: ICON, onClick: open, order: 40 });
  return { open, COMPANIES };
}
