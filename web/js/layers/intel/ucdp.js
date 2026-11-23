// Historical conflicts: UCDP Georeferenced Event Dataset, deaths per half-degree cell and period.

import { UCDP_COLOURS, INK, CONFLICT, barChart, safeGet, errorText } from './common.js';

const SRC = 'ov-intel-ucdp';
const L_DOT = 'ov-intel-ucdp-dot';
const TYPE_LABEL = { state: 'State-based conflict', nonstate: 'Non-state conflict', onesided: 'One-sided violence' };

export function registerUcdp(ctx) {
  const { map2, fmt } = ctx;
  let span = null;

  function range(period, years) {
    const [y0, y1] = years || [1989, new Date().getUTCFullYear() - 1];
    const lastFull = Math.min(y1, new Date().getUTCFullYear() - 1);
    if (period === 'current') return [y1, y1];
    if (period === '5') return [lastFull - 4, lastFull];
    if (period === '10') return [lastFull - 9, lastFull];
    if (period === 'all') return [y0, y1];
    return [lastFull, lastFull];
  }

  const layer = ctx.layers.register({
    id: 'intel.ucdp', group: 'Conflict', title: 'Historical conflicts', subtitle: 'UCDP, deaths in organised violence since 1989',
    description: 'Deaths in organised violence recorded by the Uppsala Conflict Data Program, summed per half-degree cell. Circle size shows deaths, colour the main type of violence.',
    source: 'UCDP Georeferenced Event Dataset (CC BY 4.0)', color: UCDP_COLOURS.state, symbol: 'point',
    defaultOn: false,
    options: [{ id: 'period', label: 'Period', type: 'select', default: 'last',
      choices: [{ value: 'last', label: 'Latest full year' }, { value: '5', label: 'Last 5 years' }, { value: '10', label: 'Last 10 years' },
        { value: 'all', label: 'All years since 1989' }, { value: 'current', label: 'This year so far (candidate data)' }] }],
    legend: [{ color: UCDP_COLOURS.state, label: TYPE_LABEL.state }, { color: UCDP_COLOURS.nonstate, label: TYPE_LABEL.nonstate },
      { color: UCDP_COLOURS.onesided, label: TYPE_LABEL.onesided }],
    enable() {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: L_DOT, type: 'circle', source: SRC, layout: { 'circle-sort-key': ['-', 0, ['get', 'deaths']] },
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, ['interpolate', ['linear'], ['sqrt', ['get', 'deaths']], 0, 1.5, 10, 3, 100, 12],
            7, ['interpolate', ['linear'], ['sqrt', ['get', 'deaths']], 0, 3, 10, 7, 100, 26]],
          'circle-color': ['match', ['get', 'type'], 'nonstate', UCDP_COLOURS.nonstate, 'onesided', UCDP_COLOURS.onesided, UCDP_COLOURS.state],
          'circle-opacity': 0.62, 'circle-stroke-color': INK, 'circle-stroke-width': 0.6, 'circle-pitch-alignment': 'map',
        } }, 'points');
    },
    disable() { map2.removeSource(SRC); },
    async refresh({ signal, options }) {
      let fc;
      try {
        const [y0, y1] = range(options.period, span);
        fc = await ctx.api.get('/api/intel/ucdp', { from: y0, to: y1 }, { signal });
      } catch (err) {
        if (err.code === 'BUILDING') { layer.setStatus('warn', 'The UCDP dataset is still being built (about 40 MB to download).'); return; }
        throw err;
      }
      if (!span && fc.meta?.years) {
        span = fc.meta.years;
        const [y0, y1] = range(options.period, span);
        if (y0 !== fc.meta.from || y1 !== fc.meta.to) fc = await ctx.api.get('/api/intel/ucdp', { from: y0, to: y1 }, { signal });
      }
      map2.setData(SRC, fc);
      const deaths = fc.features.reduce((s, f) => s + f.properties.deaths, 0);
      layer.setCount(fc.features.length);
      layer.setStatus('ok', `${fc.meta.from === fc.meta.to ? fc.meta.from : `${fc.meta.from}–${fc.meta.to}`}: about ${fmt.int(deaths)} deaths recorded (best estimate).`);
    },
  });
  map2.onClick([L_DOT], (f) => openCell(ctx, { kind: 'conflict_history', id: f.properties.id, title: f.properties.conflict }));
  map2.onHover([L_DOT], f => `${f.properties.conflict || 'Organised violence'}: ${fmt.int(f.properties.deaths)} deaths, ${fmt.int(f.properties.events)} events`);
  ctx.entities.handler('conflict_history', (e) => openCell(ctx, e), { group: 'Conflict' });
  return layer;
}

export async function openCell(ctx, entity) {
  const { fmt, h } = ctx;
  const key = `conflict_history:${entity.id}`;
  const card = ctx.cards.open({ key, kind: 'conflict_history', title: entity.title || 'Historical conflict', subtitle: 'Loading…', icon: '⌛', accent: CONFLICT });
  const c = await safeGet(ctx, `/api/intel/ucdp/cell/${encodeURIComponent(entity.id)}`);
  if (c.__error) {
    card.update({ subtitle: 'UCDP', sections: [{ text: errorText(c.__error) }] });
    return card;
  }
  const total = c.years.reduce((s, y) => s + y.deaths, 0);
  const events = c.years.reduce((s, y) => s + y.events, 0);
  const first = c.years[0]?.year;
  const last = c.years[c.years.length - 1]?.year;
  const series = [];
  for (let y = first; y <= last; y++) {
    const row = c.years.find(r => r.year === y);
    series.push({ label: String(y), value: row ? row.deaths : 0 });
  }
  const peak = c.years.reduce((a, b) => (b.deaths > (a?.deaths ?? -1) ? b : a), null);
  const byType = c.years.reduce((acc, y) => { acc.state += y.state; acc.nonstate += y.nonstate; acc.onesided += y.onesided; return acc; }, { state: 0, nonstate: 0, onesided: 0 });
  const title = c.conflicts?.[0] || 'Organised violence';
  const ent = { kind: 'conflict_history', id: c.id, title, subtitle: (c.countries || []).join(', '), lat: c.lat, lon: c.lon, layer: 'intel.ucdp', zoom: 7 };
  card.update({
    title, subtitle: `${(c.countries || []).join(', ')}, half-degree cell`,
    block: [{ label: 'Deaths', value: fmt.compact(total) }, { label: 'Events', value: fmt.int(events) },
      { label: 'Years', value: first === last ? String(first) : `${first}–${String(last).slice(2)}` }, { label: 'Worst year', value: peak ? String(peak.year) : '–' }],
    sections: [
      { title: 'Deaths per year (best estimate)', el: barChart(ctx, series, { colour: CONFLICT, unit: 'deaths', markMax: true, label: `Deaths per year in this cell, ${first} to ${last}` }) },
      { title: 'By type of violence', rows: [[TYPE_LABEL.state, fmt.int(byType.state)], [TYPE_LABEL.nonstate, fmt.int(byType.nonstate)],
        [TYPE_LABEL.onesided, fmt.int(byType.onesided)]] },
      c.conflicts?.length > 1 ? { title: 'Conflicts here', rows: c.conflicts.map((n, i) => [String(i + 1), n]) } : null,
      { text: `UCDP GED ${c.version}. Events geocoded only to a country are not mapped. Candidate data for the current year is provisional.` },
    ],
    links: [{ text: 'UCDP conflict data', href: 'https://ucdp.uu.se/' }],
    entity: ent, foot: 'Uppsala Conflict Data Program (CC BY 4.0). Cite: Sundberg and Melander (2013), Journal of Peace Research 50(4).',
  });
  return card;
}
