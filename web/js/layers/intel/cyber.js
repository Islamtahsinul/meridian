// Cyber: botnet command servers by hosting country (abuse.ch Feodo Tracker), internet outage signals (IODA,
// plus Cloudflare Radar outage reports and verified traffic anomalies with a token) and, with the token,
// possible BGP hijacks (arcs from the announcing network's country to the affected one) and route leaks.
// Neutral wording: "hosted in", "possible hijack", not "from" or "attack".

import { INTEL, INK, ALERT_AMBER, BGP_RAMP, NEWS_RAMP, safeGet, errorText } from './common.js';
import { openAnomaly, openBgp, openBgpPair } from './radar.js';

const SRC = 'ov-intel-cyber';
const L_C2 = 'ov-intel-cyber-c2';
const L_OUT = 'ov-intel-cyber-outage';
const L_LABEL = 'ov-intel-cyber-label';
const L_BGP = 'ov-intel-cyber-bgp';
const L_BGP_LOCAL = 'ov-intel-cyber-bgp-local';
const L_LEAK = 'ov-intel-cyber-leak';
const BGP_COLOUR = ['step', ['get', 'conf'], BGP_RAMP[0], 10, BGP_RAMP[1], 12, BGP_RAMP[2]];
const bgpFilter = (role, conf) => ['all', ['==', ['get', 'role'], role], ['>=', ['get', 'conf'], +(conf || 10)]];

export function registerCyber(ctx) {
  const { map2, fmt } = ctx;
  let recheck = null;
  const layer = ctx.layers.register({
    id: 'intel.cyber', group: 'Intelligence', title: 'Cyber', subtitle: 'Botnet servers, internet outages, BGP events',
    description: 'Botnet command servers listed by abuse.ch Feodo Tracker, by the country where they are hosted; internet outage signals from IODA (24 hours); and, with a Cloudflare Radar token, verified traffic anomalies, possible BGP hijacks and route leaks.',
    source: 'abuse.ch Feodo Tracker, IODA (Georgia Tech), Cloudflare Radar (CC BY-NC 4.0) with a token', color: INTEL, symbol: 'point',
    defaultOn: false, refreshMs: 15 * 60 * 1000,
    options: [
      { id: 'outages', label: 'Internet outage signals', type: 'toggle', default: true, refresh: false },
      { id: 'bgp', label: 'Possible BGP hijacks (Radar)', type: 'toggle', default: true, refresh: false },
      { id: 'conf', label: 'Lowest hijack confidence', type: 'select', default: '10', refresh: false,
        choices: [{ value: '8', label: '8 of 12' }, { value: '10', label: '10 of 12' }, { value: '12', label: '12 of 12' }] },
      { id: 'leaks', label: 'Route leaks (Radar)', type: 'toggle', default: false, refresh: false },
    ],
    legend: [{ color: INTEL, label: 'Botnet command servers hosted' }, { color: ALERT_AMBER, label: 'Outage signals or traffic anomaly (ring; bold while ongoing)' },
      { color: BGP_RAMP[1], label: 'Possible BGP hijack, by confidence', shape: 'line' }, { color: NEWS_RAMP[3], label: 'Route leak (small ring)' }],
    enable({ options }) {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: L_BGP, type: 'line', source: SRC, filter: bgpFilter('bgp', options.conf),
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': BGP_COLOUR, 'line-width': ['interpolate', ['linear'], ['get', 'n'], 1, 0.8, 10, 2.2],
          'line-opacity': ['case', ['>', ['get', 'ongoing'], 0], 0.75, 0.35] } }, 'lines');
      map2.addLayer({ id: L_OUT, type: 'circle', source: SRC, filter: ['==', ['get', 'role'], 'outage'],
        paint: { 'circle-radius': ['interpolate', ['linear'], ['get', 'sev'], 0, 7, 9, 18], 'circle-color': 'rgba(255,181,71,0.08)',
          'circle-stroke-color': ALERT_AMBER, 'circle-stroke-width': ['case', ['==', ['get', 'active'], 1], 2.8, 1.6],
          'circle-stroke-opacity': 0.85, 'circle-pitch-alignment': 'map' } }, 'points');
      map2.addLayer({ id: L_BGP_LOCAL, type: 'circle', source: SRC, filter: bgpFilter('bgp-local', options.conf),
        paint: { 'circle-radius': ['interpolate', ['linear'], ['get', 'n'], 1, 4, 10, 8], 'circle-color': 'rgba(0,0,0,0)', 'circle-translate': [11, -9],
          'circle-stroke-color': BGP_COLOUR, 'circle-stroke-width': 1.6, 'circle-pitch-alignment': 'map' } }, 'points');
      map2.addLayer({ id: L_LEAK, type: 'circle', source: SRC, filter: ['==', ['get', 'role'], 'leak'],
        paint: { 'circle-radius': 3.5, 'circle-color': 'rgba(0,0,0,0)', 'circle-translate': [-11, 9],
          'circle-stroke-color': NEWS_RAMP[3], 'circle-stroke-width': 1.4, 'circle-stroke-opacity': 0.8, 'circle-pitch-alignment': 'map' } }, 'points');
      map2.addLayer({ id: L_C2, type: 'circle', source: SRC, filter: ['==', ['get', 'role'], 'c2'],
        paint: { 'circle-radius': ['interpolate', ['linear'], ['sqrt', ['get', 'count']], 1, 5, 10, 22], 'circle-color': INTEL, 'circle-opacity': 0.75,
          'circle-stroke-color': INK, 'circle-stroke-width': 1.2, 'circle-pitch-alignment': 'map' } }, 'points');
      map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC, filter: ['==', ['get', 'role'], 'c2'],
        layout: { 'text-field': ['to-string', ['get', 'count']], 'text-size': 11, 'text-font': ['Open Sans Bold'], 'text-allow-overlap': true },
        paint: { 'text-color': INK } }, 'points');
      map2.setVisible(L_OUT, options.outages !== false);
      map2.setVisible(L_BGP, options.bgp !== false);
      map2.setVisible(L_BGP_LOCAL, options.bgp !== false);
      map2.setVisible(L_LEAK, !!options.leaks);
    },
    disable() { map2.removeSource(SRC); },
    async refresh({ signal }) {
      const fc = await ctx.api.get('/api/intel/cyber', null, { signal });
      map2.setData(SRC, fc);
      const by = (r) => fc.features.filter(f => f.properties.role === r);
      const c2 = by('c2');
      const servers = c2.reduce((s, f) => s + f.properties.count, 0);
      const outages = by('outage').length;
      const minConf = +(layer.option('conf') || 10);
      const nKey = minConf >= 12 ? 'n12' : minConf >= 10 ? 'n10' : 'n';
      const hijacks = [...by('bgp'), ...by('bgp-local')].reduce((s, f) => s + (f.properties[nKey] || 0), 0);
      layer.setCount(servers);
      const meta = fc.meta || {};
      const radar = meta.cloudflare_token ? (meta.radar_at ? `, ${fmt.int(hijacks)} possible BGP hijacks (confidence ${minConf}+) in 24 h` : ', Cloudflare Radar loading')
        : '. Cloudflare Radar needs a token';
      layer.setStatus('ok', `${fmt.int(servers)} listed server${servers === 1 ? '' : 's'} in ${c2.length} countr${c2.length === 1 ? 'y' : 'ies'}`
        + (meta.outages_at?.ioda ? `, ${outages} countries with outage signals` : ', checking outage signals…') + radar + '.');
      if ((!meta.outages_at?.ioda || (meta.cloudflare_token && !meta.radar_at)) && !recheck) {
        recheck = setTimeout(() => { recheck = null; if (layer.isOn()) layer.refresh('outages'); }, 20000);
      }
    },
    onOption(id, value) {
      if (id === 'conf' && ctx.map.getLayer(L_BGP)) {
        ctx.map.setFilter(L_BGP, bgpFilter('bgp', value));
        ctx.map.setFilter(L_BGP_LOCAL, bgpFilter('bgp-local', value));
      }
      if (id === 'outages') map2.setVisible(L_OUT, !!value);
      if (id === 'bgp') { map2.setVisible(L_BGP, !!value); map2.setVisible(L_BGP_LOCAL, !!value); }
      if (id === 'leaks') map2.setVisible(L_LEAK, !!value);
    },
  });
  map2.onClick([L_C2, L_LABEL], (f) => openCyber(ctx, { kind: 'cyber', id: `feodo:${f.properties.iso2}` }));
  map2.onClick([L_OUT], (f) => openCyber(ctx, { kind: 'outage', id: `ioda:${f.properties.iso2}` }));
  map2.onClick([L_BGP, L_BGP_LOCAL], (f) => openBgpPair(ctx, f.properties.pair));
  map2.onClick([L_LEAK], (f) => openCyber(ctx, { kind: 'cyber', id: `leaks:${f.properties.iso2}` }));
  map2.onHover([L_C2, L_LABEL], f => `Botnet command servers hosted in ${f.properties.name}: ${f.properties.count}`);
  map2.onHover([L_OUT], f => `Internet outage signals in ${f.properties.name} (${f.properties.src})`);
  map2.onHover([L_BGP, L_BGP_LOCAL], f => (f.properties.a === f.properties.b
    ? `Possible BGP hijacks within ${f.properties.bn} in 24 h: ${f.properties.n}, highest confidence ${f.properties.conf} of 12`
    : `Possible BGP hijacks, ${f.properties.an} to ${f.properties.bn}, in 24 h: ${f.properties.n}, highest confidence ${f.properties.conf} of 12`));
  map2.onHover([L_LEAK], f => `Route leaks by networks in ${f.properties.name}: ${f.properties.n}, seen by up to ${f.properties.peers} peers`);
  ctx.entities.handler('cyber', (e) => openCyber(ctx, e), { group: 'Intelligence' });
  ctx.entities.handler('outage', (e) => openCyber(ctx, e), { group: 'Intelligence' });
  return layer;
}

export async function openCyber(ctx, entity) {
  const { fmt, h } = ctx;
  const sid = String(entity.id || '');
  if (sid.startsWith('cfr:')) return openAnomaly(ctx, entity);
  if (sid.startsWith('cfbgp:') || sid.startsWith('cfleak:')) return openBgp(ctx, entity);
  const [src, iso2raw] = sid.split(':');
  const iso2 = (iso2raw || '').toUpperCase();
  if (src === 'leaks') return openLeaks(ctx, iso2);
  const kind = entity.kind === 'outage' ? 'outage' : 'c2';
  const key = `${entity.kind}:${entity.id}`;
  const card = ctx.cards.open({ key, kind: entity.kind, title: entity.title || (kind === 'c2' ? 'Botnet command servers' : 'Internet outage signals'),
    subtitle: 'Loading…', icon: kind === 'c2' ? '⌁' : '◌', accent: INTEL });
  const d = await safeGet(ctx, `/api/intel/cyber/${kind}/${encodeURIComponent(iso2)}`);
  if (d.__error) {
    card.update({ subtitle: iso2, sections: [{ text: d.__error.status === 404 ? 'Nothing is listed for this country right now.' : errorText(d.__error) }] });
    return card;
  }
  if (kind === 'c2') {
    const ent = ctx.dom.validLatLon(d.lat, d.lon) ? { kind: 'cyber', id: `feodo:${d.iso2}`, title: `Botnet servers hosted in ${d.name}`, lat: d.lat, lon: d.lon, layer: 'intel.cyber', zoom: 4 } : null;
    card.update({
      title: `Botnet command servers hosted in ${d.name}`, subtitle: 'abuse.ch Feodo Tracker blocklist',
      block: [{ label: 'Listed', value: fmt.int(d.count) }, { label: 'Online now', value: fmt.int(d.online) },
        { label: 'Malware families', value: fmt.int(d.malware.length) }],
      sections: [
        { title: 'Malware', rows: d.malware.map(([m, n]) => [m, fmt.int(n)]) },
        { title: 'Hosting networks', rows: d.networks.map(([n, c]) => [n, fmt.int(c)]) },
        { title: 'Listed servers', list: d.entries.slice(0, 12).map(e => ({ title: `${e.ip}:${e.port}`, subtitle: `${e.malware}, ${e.status}, last online ${e.last_online || 'unknown'}`, detail: e.first_seen ? `since ${e.first_seen.slice(0, 10)}` : '' })) },
        { text: 'Servers are listed by where they are hosted, which says nothing about who runs them. Hosting providers usually take them down once notified.' },
      ],
      links: [{ text: 'Feodo Tracker', href: 'https://feodotracker.abuse.ch/' }],
      entity: ent, foot: `abuse.ch Feodo Tracker blocklist${d.updated ? `, fetched ${fmt.ago(d.updated)}` : ''}.`,
    });
  } else {
    const io = d.ioda;
    const an = d.anomalies || [];
    const ent = ctx.dom.validLatLon(d.lat, d.lon) ? { kind: 'outage', id: `ioda:${d.iso2}`, title: `Internet outage signals in ${d.name}`, lat: d.lat, lon: d.lon, layer: 'intel.cyber', zoom: 4 } : null;
    const block = io ? [{ label: 'Outage events, 24 h', value: fmt.int(io.events) }, { label: 'Signal score', value: fmt.compact(io.score) },
      { label: 'Severity', value: (+io.severity).toFixed(1), unit: '/10' }] : [{ label: 'Radar anomalies, 7 days', value: fmt.int(an.length) },
      { label: 'Outage reports', value: fmt.int(d.cloudflare.length) }];
    card.update({
      title: `Internet outage signals in ${d.name}`, subtitle: [io ? 'IODA, last 24 hours' : null, an.length || d.cloudflare.length ? 'Cloudflare Radar' : null].filter(Boolean).join(', '),
      badges: [{ text: 'Automated detection', tone: 'grey' }, an.some(a => a.active) ? { text: 'Radar anomaly ongoing', tone: 'amber' } : null].filter(Boolean),
      block,
      sections: [
        io ? { title: 'IODA', rows: [['Signals that dropped', io.methods.join(', ')], ['Outage events in 24 hours', fmt.int(io.events)]] } : null,
        an.length ? { title: 'Cloudflare Radar traffic anomalies, 7 days', list: an.map(a => ({ title: a.title, subtitle: `${a.status_words}, ${a.end ? fmt.utc(a.start).slice(5, 16) : 'ongoing'}`,
          detail: fmt.ago(a.start), onClick: () => openAnomaly(ctx, { kind: 'outage', id: `cfr:${a.id}`, title: a.title }) })) } : null,
        d.cloudflare?.length ? { title: 'Cloudflare Radar outage reports', list: d.cloudflare.map(o => ({ title: o.description || `${o.type} outage`, subtitle: [o.cause, o.start ? `from ${o.start.slice(0, 16).replace('T', ' ')}` : null].filter(Boolean).join(', ') })) } : null,
        !d.token ? { text: 'Cloudflare Radar anomalies need CLOUDFLARE_RADAR_TOKEN on the server.' } : null,
        { text: 'IODA flags unusual drops in routing, active probing and traffic signals; Cloudflare Radar flags drops in the traffic it serves. Drops can come from power cuts, cable faults, shutdowns or measurement gaps.' },
      ],
      links: [{ text: 'IODA dashboard', href: `https://ioda.inetintel.cc.gatech.edu/country/${d.iso2}` },
        an.length ? { text: 'Cloudflare Radar', href: `https://radar.cloudflare.com/traffic/${d.iso2.toLowerCase()}` } : null].filter(Boolean),
      entity: ent, foot: `IODA (Georgia Tech)${an.length ? ', Cloudflare Radar (CC BY-NC 4.0)' : ''}${d.updated ? `, checked ${fmt.ago(d.updated)}` : ''}.`,
    });
  }
  return card;
}

async function openLeaks(ctx, iso2) {
  const { fmt } = ctx;
  const card = ctx.cards.open({ key: `cyber:leaks:${iso2}`, kind: 'cyber', title: 'Route leaks', subtitle: 'Loading…', icon: '⇄', accent: INTEL });
  const d = await safeGet(ctx, '/api/intel/radar', { country: iso2 });
  if (d.__error) {
    card.update({ subtitle: 'Cloudflare Radar', sections: [{ text: errorText(d.__error) }] });
    return card;
  }
  const leaks = d.leaks || [];
  card.update({
    title: `Route leaks involving networks in ${iso2}`, subtitle: 'Cloudflare Radar, last 24 hours, the most widely seen first',
    badges: [{ text: 'Automated detection', tone: 'grey' }],
    block: [{ label: 'Leaks', value: fmt.int(d.leaks_count || leaks.length) }, { label: 'Most peers', value: fmt.int(Math.max(0, ...leaks.map(l => l.peers))) }],
    sections: [{ list: leaks.map(l => ({ title: l.title, subtitle: `seen by ${l.peers} peers, ${l.prefixes} prefixes, ${l.finished ? 'finished' : 'ongoing'}`,
      detail: fmt.ago(l.last), onClick: () => openBgp(ctx, { kind: 'cyber', id: `cfleak:${l.id}`, title: l.title }) })), empty: 'None in the last day.' },
    { text: 'A route leak is a network passing on routes it should have kept to itself. Most are short-lived configuration mistakes.' }],
    links: [{ text: 'Cloudflare Radar routing', href: 'https://radar.cloudflare.com/routing' }], foot: 'Cloudflare Radar, CC BY-NC 4.0.',
  });
  return card;
}
