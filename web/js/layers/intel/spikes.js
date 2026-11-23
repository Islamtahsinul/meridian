// News spikes: per-country GDELT volume against its own baseline. The "Unusual news volume" list in the
// News drawer, the spike card (entity kind news_spike) and the country-card section. Wording stays factual:
// "unusually high news volume", never "crisis".

import { INTEL, CONFLICT, PROTEST_COLOUR, barChart, safeGet, errorText, short } from './common.js';

const METRIC_COLOUR = { volume: INTEL, conflict: CONFLICT, protest: PROTEST_COLOUR };
const METRIC_NAME = { volume: 'News volume', conflict: 'Conflict reports', protest: 'Protest reports' };
const INNOCENT = 'GDELT is machine-coded from news and can spike when one story is widely repeated, when a big outlet '
  + 'publishes many pieces about one place, or around elections, summits, anniversaries and sports events. Unusual volume '
  + 'says the country is in the news more than usual, not what is happening there.';

function dayLabel(d) {
  const s = String(d);
  const m = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][+s.slice(4, 6) - 1] || '';
  return `${+s.slice(6, 8)} ${m}`;
}

function ratioText(r, median = 1) {
  // a ratio against a usual level under 1 a day says little ("2 times usual" for 1 report against 0)
  return r == null || median == null || median < 1 ? '' : `${(+r).toFixed(1)} times usual`;
}

function chart(ctx, series, metric, now) {
  const items = (series || []).filter(p => metric !== 'protest' || p.protest != null)
    .map(p => ({ label: dayLabel(p.day), value: p[metric] || 0 }));
  if (now != null) items.push({ label: 'last 24 h', value: now });
  if (items.length < 3) return null;
  return barChart(ctx, items, { colour: METRIC_COLOUR[metric], height: 54, label: `${METRIC_NAME[metric]} per day`, markMax: true });
}

/** The "Unusual news volume" block at the top of the News drawer. */
export async function renderSpikeList(ctx, box) {
  const { h, fmt } = ctx;
  const res = await safeGet(ctx, '/api/intel/spikes', { days: 2 });
  ctx.dom.clear(box);
  const head = h('div', { class: 'intel-th-head' }, h('h3', null, 'Unusual news volume'));
  box.append(head);
  if (res.__error) {
    box.append(h('p', { class: 'intel-none' }, `Unavailable: ${errorText(res.__error)}`));
    return;
  }
  if (res.state !== 'ok') {
    box.append(h('p', { class: 'intel-none' }, res.message || 'Waiting for GDELT history.'));
    return;
  }
  const flags = (res.flags || []).slice(0, 8);
  head.append(h('span', { class: 'intel-th-count tnum' }, `${fmt.int((res.flags || []).filter(f => f.level === 'unusual').length)} unusual`));
  if (!flags.length) {
    box.append(h('p', { class: 'intel-none' }, 'Nothing unusual: no country is far above its own baseline right now.'));
    return;
  }
  const ul = h('ul', { class: 'intel-items' });
  for (const f of flags) {
    const pill = h('span', { class: `intel-sev ${f.level === 'unusual' ? 'amber' : 'blue'}`, title: f.level === 'unusual' ? 'Unusual' : 'Notable' },
      `${(+f.ratio).toFixed(1)}×`);
    const btn = h('button', { type: 'button', class: 'intel-item-main' }, pill,
      h('span', { class: 'intel-item-text' }, h('span', { class: 'intel-item-title' }, `${f.name}: ${f.label}`),
        h('span', { class: 'intel-item-meta' }, [`${fmt.int(f.value)} in 24 h, usual ${fmt.int(f.baseline)}`, f.reason, f.level === 'notable' ? 'notable' : null]
          .filter(Boolean).join(', '))));
    btn.addEventListener('click', () => openSpikeCountry(ctx, f.iso2, f.metric, f.name));
    ul.append(h('li', { class: 'intel-item' }, btn));
  }
  box.append(ul);
}

/** Card for a spike episode (event entity, id like "SD:conflict:1795450800"). */
export async function openSpike(ctx, entity) {
  const { fmt } = ctx;
  const key = `news_spike:${entity.id}`;
  const card = ctx.cards.open({ key, kind: 'news_spike', title: entity.title || 'Unusual news volume', subtitle: 'Loading…', icon: '▲', accent: INTEL });
  const d = await safeGet(ctx, `/api/intel/spikes/${encodeURIComponent(entity.id)}`);
  if (d.__error) {
    card.update({ subtitle: 'News spike', sections: [{ text: errorText(d.__error) }] });
    return card;
  }
  const det = d.detail || {};
  const metric = d.metric;
  const nowM = d.country_now?.metrics?.[metric];
  const ch = chart(ctx, d.country_now?.series, metric, nowM?.value);
  card.update({
    title: d.entity?.title || det.title || 'Unusual news volume', subtitle: `${d.name}, ${fmt.utc(d.first_t).slice(0, 16)} to ${fmt.utc(d.last_t).slice(11, 16)} UTC`,
    icon: d.flag || '▲', badges: [{ text: 'Unusual for this country', tone: 'amber' }, { text: 'Machine-coded', tone: 'grey' }, det.reason ? { text: det.reason, tone: 'grey' } : null].filter(Boolean),
    block: [{ label: 'Last 24 h', value: fmt.int(d.value) }, { label: 'Usual', value: fmt.int(d.baseline) },
      { label: 'Ratio', value: (+d.ratio).toFixed(1), unit: '×' }, { label: 'Peak', value: (+d.peak_ratio).toFixed(1), unit: '×' }],
    sections: [
      { text: det.text || null },
      ch ? { title: `${METRIC_NAME[metric]} per day`, el: ch } : null,
      { rows: [['Measure', det.label], ['Test', det.method === 'Poisson' ? `Poisson tail, p ${d.p}` : `robust z ${d.z}`],
        ['Baseline', det.baseline_days ? `median of ${det.baseline_days} days, adjusted for total GDELT volume` : null],
        ['First flagged', fmt.utc(d.first_t)], ['Last flagged', fmt.utc(d.last_t)],
        ['Now', nowM ? [`${fmt.int(nowM.value)} in 24 h`, ratioText(nowM.ratio, nowM.median)].filter(Boolean).join(', ') : null]] },
      { title: 'Why this may be innocent', text: INNOCENT },
    ],
    actions: [{ text: 'Country profile', onClick: () => ctx.entities.open({ kind: 'country', id: d.iso2, title: d.name }, { fly: false }) }],
    entity: d.entity || null, foot: 'Computed here from GDELT 2.0 exports (open data, credit the GDELT Project).',
  });
  return card;
}

/** Card for a country's current reading (from the drawer list, notable or unusual). */
export async function openSpikeCountry(ctx, iso2, metric = 'volume', name = null) {
  const { fmt } = ctx;
  const card = ctx.cards.open({ key: `news_spike:now:${iso2}`, kind: 'news_spike', title: name ? `${name}: news volume` : 'News volume', subtitle: 'Loading…', icon: '▲', accent: INTEL });
  const d = await safeGet(ctx, `/api/intel/spikes/country/${encodeURIComponent(iso2)}`);
  if (d.__error) {
    card.update({ subtitle: 'News volume', sections: [{ text: errorText(d.__error) }] });
    return card;
  }
  const flag = (d.flags || []).find(f => f.metric === metric) || (d.flags || [])[0];
  const m = d.metrics?.[flag?.metric || metric] || {};
  const ch = chart(ctx, d.series, flag?.metric || metric, m.value);
  const pos = flag && ctx.dom.validLatLon(flag.lat, flag.lon) ? { lat: flag.lat, lon: flag.lon } : null;
  card.update({
    title: flag ? flag.title : `${d.name}: ${METRIC_NAME[metric].toLowerCase()}`, subtitle: `${d.name}, last 24 hours against its own baseline`,
    badges: [flag ? { text: flag.level === 'unusual' ? 'Unusual for this country' : 'Notable', tone: flag.level === 'unusual' ? 'amber' : 'blue' } : null,
      flag?.reason ? { text: flag.reason, tone: 'grey' } : null, { text: 'Machine-coded', tone: 'grey' }].filter(Boolean),
    block: [{ label: 'Last 24 h', value: fmt.int(m.value || 0) }, { label: 'Usual', value: m.median != null ? fmt.int(m.median) : '–' },
      { label: 'Ratio', value: m.ratio != null ? (+m.ratio).toFixed(1) : '–', unit: '×' }],
    sections: [
      flag ? { text: flag.text } : (d.message ? { text: d.message } : null),
      ch ? { title: `${METRIC_NAME[flag?.metric || metric]} per day`, el: ch } : null,
      { title: 'All measures', rows: Object.entries(d.metrics || {}).map(([k, v]) => [METRIC_NAME[k],
        [`${fmt.int(v.value || 0)} in 24 h`, v.median != null ? `usual ${fmt.int(v.median)}` : null, ratioText(v.ratio, v.median) || null,
          v.level ? `(${v.level})` : null].filter(Boolean).join(', ').replace(', (', ' (')]) },
      { title: 'Why this may be innocent', text: INNOCENT },
    ],
    actions: [{ text: 'Country profile', onClick: () => ctx.entities.open({ kind: 'country', id: iso2, title: d.name }, { fly: false }) },
      pos ? { text: 'Show the country', onClick: () => ctx.map2.flyTo(pos.lat, pos.lon, 4) } : null].filter(Boolean),
    foot: 'Computed here from GDELT 2.0 exports (open data, credit the GDELT Project).',
  });
  return card;
}

/** The country card's "News volume" section. */
export function spikeSection(ctx, sp) {
  const { h, fmt } = ctx;
  if (!sp) return null;
  const el = h('div', { class: 'intel-spk' });
  if (sp.state !== 'ok' && sp.message) el.append(h('p', { class: 'dim' }, sp.message));
  const dl = h('dl', { class: 'kv' });
  for (const [k, v] of Object.entries(sp.metrics || {})) {
    if (v.value == null) continue;
    const tail = [v.median != null && v.n >= 14 ? `usual ${fmt.int(v.median)}` : null, v.ratio != null && v.n >= 14 ? (ratioText(v.ratio, v.median) || null) : null,
      v.level ? (v.level === 'unusual' ? 'unusual' : 'notable') : null, v.reason].filter(Boolean).join(', ');
    dl.append(h('dt', null, METRIC_NAME[k]), h('dd', { class: v.level ? 'intel-spk-flag' : null }, `${fmt.int(v.value)} in 24 h${tail ? `, ${tail}` : ''}`));
  }
  if (dl.children.length) el.append(dl);
  const flagged = Object.entries(sp.metrics || {}).find(([, v]) => v.level)?.[0] || 'volume';
  const ch = chart(ctx, sp.series, flagged, sp.metrics?.[flagged]?.value);
  if (ch) el.append(h('div', { class: 'intel-spk-chart-label' }, `${METRIC_NAME[flagged]} per day`), ch);
  for (const e of (sp.episodes || []).slice(0, 3)) {
    const b = h('button', { type: 'button', class: 'text-btn' }, `${METRIC_NAME[e.metric]} ${(+e.peak_ratio).toFixed(1)} times usual, ${fmt.ago(e.last_t)}`);
    b.addEventListener('click', () => openSpike(ctx, { kind: 'news_spike', id: e.id }));
    el.append(h('div', null, b));
  }
  return { title: 'News volume, last 24 hours', el };
}

export function registerSpikes(ctx) {
  ctx.entities.handler('news_spike', (e) => (String(e.id || '').split(':').length === 3 ? openSpike(ctx, e) : openSpikeCountry(ctx, e.iso2 || e.id)),
    { group: 'Intelligence', icon: '▲' });
}

export { short };
