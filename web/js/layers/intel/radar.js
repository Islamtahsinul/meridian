// Cloudflare Radar (needs CLOUDFLARE_RADAR_TOKEN on the server): verified traffic anomalies, possible BGP
// hijacks and route leaks. Cards and the country-card "Internet" section; the map symbols live in cyber.js.

import { INTEL, safeGet, errorText, short } from './common.js';

const STATUS_TONE = { VERIFIED: 'green', TP: 'blue', UNVERIFIED: 'grey' };

function span(fmt, a) {
  if (!a.start) return '';
  return a.end ? `${fmt.utc(a.start).slice(5, 16)} to ${fmt.utc(a.end).slice(5, 16)} UTC` : `since ${fmt.utc(a.start).slice(5, 16)} UTC, ongoing`;
}

function duration(s) {
  if (!s && s !== 0) return null;
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

export async function openAnomaly(ctx, entity) {
  const { fmt } = ctx;
  const uid = String(entity.id || '').replace(/^cfr:/, '');
  const card = ctx.cards.open({ key: `outage:cfr:${uid}`, kind: 'outage', title: entity.title || 'Internet traffic anomaly', subtitle: 'Loading…', icon: '◌', accent: INTEL });
  const d = await safeGet(ctx, `/api/intel/radar/anomaly/${encodeURIComponent(uid)}`);
  if (d.__error) {
    card.update({ subtitle: 'Cloudflare Radar', sections: [{ text: errorText(d.__error) }] });
    return card;
  }
  const ann = d.annotation;
  const ent = ctx.dom.validLatLon(d.lat, d.lon) ? { kind: 'outage', id: `cfr:${d.id}`, title: d.title, lat: d.lat, lon: d.lon, layer: 'intel.cyber', zoom: 4 } : null;
  card.update({
    title: d.title, subtitle: `Cloudflare Radar, ${span(fmt, d)}`, icon: d.flag || '◌',
    badges: [{ text: d.status_words, tone: STATUS_TONE[d.status] || 'grey' }, d.active ? { text: 'Ongoing', tone: 'amber' } : null,
      { text: d.type === 'country' ? 'Whole country' : 'One network', tone: 'grey' }].filter(Boolean),
    block: [{ label: 'Started', value: fmt.ago(d.start) }, { label: 'Lasted', value: d.end ? duration(d.end - d.start) : 'ongoing' },
      { label: 'Severity', value: (+d.severity).toFixed(1), unit: '/10' }],
    sections: [
      { rows: [['Country', d.name], ['Network', d.asn ? `AS${d.asn}${d.as_name ? ` (${d.as_name})` : ''}` : null],
        ['Seen in', (d.sources || []).join(', ') || null], ['Cloudflare status', d.status]] },
      ann ? { title: 'Cloudflare’s outage report', rows: [['What happened', ann.description || null], ['Cause', ann.cause || null], ['Scope', ann.scope_type || null]] } : null,
      { title: 'About this data', text: 'Cloudflare Radar flags drops in the traffic it sees from a country or network against its usual level. Drops can come from power cuts, cable faults, shutdowns or network faults. TP means verified by Radar; VERIFIED means verified by Radar and a third-party source.' },
    ],
    links: [{ text: 'Cloudflare Radar', href: d.url }, ann?.url ? { text: 'Radar’s post about it', href: ann.url } : null].filter(Boolean),
    entity: ent, foot: 'Cloudflare Radar, CC BY-NC 4.0.',
  });
  return card;
}

function hijackRows(fmt, d) {
  return [['Announced by', d.hijacker_asn ? `AS${d.hijacker_asn}${d.hijacker_name ? ` (${d.hijacker_name})` : ''}${d.hijacker_country ? `, ${d.hijacker_country}` : ''}` : null],
    ['Affected networks', (d.victim_asns || []).map((a, i) => `AS${a}${d.victim_names?.[i] ? ` (${d.victim_names[i]})` : ''}`).join(', ') || null],
    ['Affected countries', (d.victim_countries || []).join(', ') || null],
    ['Prefixes', (d.prefixes || []).slice(0, 6).join(', ') + ((d.n_prefixes || 0) > 6 ? ` and ${d.n_prefixes - 6} more` : '') || null],
    ['First seen', fmt.utc(d.start)], ['Last seen', `${fmt.utc(d.last)} (${fmt.ago(d.last)})`], ['Duration', duration(d.duration_s)],
    ['Seen by', d.peers ? `${fmt.int(d.peers)} route collectors' peers` : null],
    ['Signals', (d.tags || []).map(t => `${t.name.replace(/_/g, ' ')} (${t.score})`).join(', ') || null]];
}

export async function openBgp(ctx, entity) {
  const { h, fmt } = ctx;
  const id = String(entity.id || '');
  const kind = id.startsWith('cfleak:') ? 'leak' : 'hijack';
  const num = id.replace(/^cf(bgp|leak):/, '');
  const card = ctx.cards.open({ key: `cyber:${id}`, kind: 'cyber', title: entity.title || (kind === 'leak' ? 'Route leak' : 'Possible BGP hijack'),
    subtitle: 'Loading…', icon: '⇄', accent: INTEL });
  const d = await safeGet(ctx, `/api/intel/radar/bgp/${kind}/${encodeURIComponent(num)}`);
  if (d.__error) {
    card.update({ subtitle: 'Cloudflare Radar', sections: [{ text: errorText(d.__error) }] });
    return card;
  }
  const ent = ctx.dom.validLatLon(d.lat, d.lon) ? { kind: 'cyber', id, title: d.title, lat: d.lat, lon: d.lon, layer: 'intel.cyber', zoom: 4 } : null;
  if (kind === 'hijack') {
    card.update({
      title: d.title, subtitle: `Cloudflare Radar BGP detection ${d.id}`,
      badges: [{ text: `Confidence ${d.confidence} of 12`, tone: d.confidence >= 10 ? 'amber' : 'grey' }, d.ongoing ? { text: 'Ongoing', tone: 'amber' } : null,
        { text: 'Automated detection', tone: 'grey' }].filter(Boolean),
      block: [{ label: 'Confidence', value: String(d.confidence), unit: '/12' }, { label: 'Prefixes', value: fmt.int(d.n_prefixes || 0) },
        { label: 'Seen', value: fmt.ago(d.last) }],
      sections: [{ rows: hijackRows(fmt, d) },
        { title: 'Why this may be innocent', text: 'A network can start announcing another network’s addresses by mistake, through a customer or sibling relationship, or after a move that the registries do not show yet. Detections with low confidence are often such cases; RPKI and IRR signals help tell them apart.' }],
      links: [{ text: 'Cloudflare Radar routing', href: d.url }], entity: ent, foot: 'Cloudflare Radar BGP hijack detection, CC BY-NC 4.0.',
    });
  } else {
    card.update({
      title: d.title, subtitle: `Cloudflare Radar route leak ${d.id}`,
      badges: [{ text: d.finished ? 'Finished' : 'Ongoing', tone: d.finished ? 'grey' : 'amber' }, { text: 'Automated detection', tone: 'grey' }],
      block: [{ label: 'Peers that saw it', value: fmt.int(d.peers) }, { label: 'Prefixes', value: fmt.int(d.prefixes) }, { label: 'Seen', value: fmt.ago(d.last) }],
      sections: [{ rows: [['Leaking network', d.leak_asn ? `AS${d.leak_asn}${d.leak_name ? ` (${d.leak_name})` : ''}` : null], ['Country', d.country],
        ['Path', (d.segment || []).map(s => `AS${s.asn}${s.name ? ` (${short(s.name, 28)})` : ''}`).join(' to ') || null],
        ['Origins', fmt.int(d.origins)], ['Leaked routes', fmt.int(d.leaks)], ['Detected', fmt.utc(d.start)], ['Last seen', fmt.utc(d.last)]] },
        { title: 'What this means', text: 'A route leak is a network passing on routes it should have kept to itself, so traffic may take a longer or unexpected path. Most leaks are short-lived configuration mistakes.' }],
      links: [{ text: 'Cloudflare Radar routing', href: d.url }], entity: ent, foot: 'Cloudflare Radar route leak detection, CC BY-NC 4.0.',
    });
  }
  return card;
}

/** Card for a map arc: every possible hijack from one country's networks into another's in the last day. */
export async function openBgpPair(ctx, pair) {
  const { fmt } = ctx;
  const card = ctx.cards.open({ key: `cyber:pair:${pair}`, kind: 'cyber', title: 'Possible BGP hijacks', subtitle: 'Loading…', icon: '⇄', accent: INTEL });
  const d = await safeGet(ctx, `/api/intel/radar/bgp/pair/${encodeURIComponent(pair)}`);
  if (d.__error) {
    card.update({ subtitle: 'Cloudflare Radar', sections: [{ text: errorText(d.__error) }] });
    return card;
  }
  const same = d.from === d.to;
  card.update({
    title: same ? `Possible BGP hijacks within ${d.to_name}` : `Possible BGP hijacks, ${d.from_name} to ${d.to_name}`,
    subtitle: 'Cloudflare Radar, last 24 hours, confidence 8 or more',
    badges: [{ text: 'Automated detection', tone: 'grey' }],
    block: [{ label: 'Detections', value: fmt.int(d.hijacks.length) }, { label: 'Ongoing', value: fmt.int(d.hijacks.filter(x => x.ongoing).length) },
      { label: 'Highest confidence', value: String(Math.max(0, ...d.hijacks.map(x => x.confidence))), unit: '/12' }],
    sections: [{ title: 'Detections', list: d.hijacks.slice(0, 20).map(x => ({ title: x.title, subtitle: `confidence ${x.confidence}, ${x.ongoing ? 'ongoing' : `last seen ${fmt.ago(x.last)}`}`,
      detail: x.prefixes?.[0] || '', onClick: () => openBgp(ctx, { kind: 'cyber', id: `cfbgp:${x.id}`, title: x.title }) })), empty: 'None in the last day.' },
    { text: 'The line runs from the country of the network that announced the addresses to the country of the network that holds them. Detections include false positives.' }],
    links: [{ text: 'Cloudflare Radar routing', href: 'https://radar.cloudflare.com/routing' }], foot: 'Cloudflare Radar, CC BY-NC 4.0.',
  });
  return card;
}

/** The country card's "Internet (Cloudflare Radar)" section, or null when there is nothing to say. */
export function internetSection(ctx, net) {
  const { fmt } = ctx;
  if (!net) return null;
  if (!net.token) return { title: 'Internet (Cloudflare Radar)', text: 'Needs CLOUDFLARE_RADAR_TOKEN on the server for traffic anomalies and BGP events.' };
  const items = [];
  for (const a of net.anomalies || []) {
    items.push({ title: a.title, subtitle: `${a.status_words}, ${a.end ? `${fmt.utc(a.start).slice(5, 16)} to ${fmt.utc(a.end).slice(11, 16)} UTC` : 'ongoing'}`,
      detail: a.active ? 'now' : fmt.ago(a.start), onClick: () => openAnomaly(ctx, { kind: 'outage', id: `cfr:${a.id}`, title: a.title }) });
  }
  for (const x of (net.hijacks || []).slice(0, 4)) {
    items.push({ title: x.title, subtitle: `confidence ${x.confidence} of 12${x.ongoing ? ', ongoing' : ''}`, detail: fmt.ago(x.last),
      onClick: () => openBgp(ctx, { kind: 'cyber', id: `cfbgp:${x.id}`, title: x.title }) });
  }
  const text = `${fmt.int((net.anomalies || []).length)} verified traffic anomalies in 7 days. In the last 24 hours: `
    + `${fmt.int(net.hijacks_as_victim || 0)} possible BGP hijacks of networks here, ${fmt.int(net.hijacks_as_origin || 0)} announced from `
    + `networks here, and ${fmt.int(net.leaks_count || 0)} route leaks involving networks here.`;
  return { title: 'Internet (Cloudflare Radar)', text, list: items.length ? items : null };
}
