// Cards owned by the ships module: ship (id = MMSI), strait, port activity and SDN vessel records.

import { GROUP_COLOR, classOf, degrees, etaText, hours, knots, stateLabel } from './common.js';
import { vesselSection } from './anomalies.js';
import { openChokepoint, portwatchBadges, portwatchSection } from './portwatch.js';

const SHIP_ICON = '⚓︎';
const STRAIT_ICON = '⇄';

// ------------------------------------------------------------------ ship card
export function shipSubtitle(d) {
  const bits = [];
  if (d.type_label && d.type_label !== 'Type not reported') bits.push(d.type_label);
  else if (d.cls_label) bits.push(d.cls_label === 'Other or unknown' ? 'Vessel' : d.cls_label);
  if (d.flag_name) bits.push(d.flag_name);
  return bits.join(', ');
}

function shipEntity(d) {
  const pos = d.dark ? d.dark.last : d.position;
  const e = { kind: 'ship', id: String(d.mmsi), title: d.title, subtitle: shipSubtitle(d), layer: 'ships', zoom: 11 };
  if (pos && pos.lat != null) { e.lat = pos.lat; e.lon = pos.lon; }
  return e;
}

// Which sanctions lists name the vessel: `lists` when the record has it, otherwise read from the programme
// labels (the sanctions module puts "UK: ", "UN: " or "EU: " in front of other lists' programmes).
const LIST_NAMES = { OFAC: 'the US Treasury OFAC SDN list', UK: 'the UK Sanctions List', UN: 'the UN Security Council consolidated list', EU: 'the EU consolidated financial sanctions list' };
function sanctionLists(s) {
  if (Array.isArray(s?.lists) && s.lists.length) return s.lists;
  const out = [];
  for (const p of s?.programs || []) {
    const id = /^(UK|UN|EU): /.exec(p)?.[1] || 'OFAC';
    if (!out.includes(id)) out.push(id);
  }
  return out.length ? out : ['OFAC'];
}
function listsSentence(lists) {
  const names = lists.map((id) => LIST_NAMES[id] || id);
  return names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}` : names[0];
}

function badges(ctx, d) {
  const out = [];
  if (d.sanctions_strength === 'confirmed') {
    out.push({ text: `Sanctioned (${sanctionLists(d.sanctions).join(', ')})`, tone: 'red', title: (d.sanctions.programs || []).join(', ') });
  } else if (d.sanctions_strength === 'possible') {
    out.push({ text: 'Possible sanctions match', tone: 'amber', title: 'Shares a name or MMSI with a listed vessel, but the IMO does not confirm it.' });
  }
  if (d.dark) {
    out.push({ text: `Dark for ${hours(d.dark.silent_s)}`, tone: 'amber', title: 'Silent while vessels nearby keep reporting' });
    out.push({ text: 'Estimated position', tone: 'amber' });
  }
  if (d.suspect) out.push({ text: 'Position suspect', tone: 'amber', title: 'Reported speed or position is impossible: likely GNSS interference' });
  if (d.known && !d.live && d.position) out.push({ text: `Last seen ${ctx.fmt.ago(d.position.t)}`, tone: 'grey' });
  else if (d.stale && d.position) out.push({ text: `Last report ${ctx.fmt.ago(d.position.t)}`, tone: 'grey' });
  if (d.strait) out.push({ text: `In ${d.strait.name}`, tone: 'blue' });
  if (d.ais_class === 'B') out.push({ text: 'Class B transponder', tone: 'grey' });
  const sts = (d.anomalies || []).find(a => a.kind === 'sts' && a.published && a.ongoing);
  if (sts) out.push({ text: 'Possible ship-to-ship transfer', tone: 'amber', title: sts.subtitle });
  return out;
}

function block(ctx, d) {
  const p = d.position || {};
  const cog = d.moving ? p.cog : null;      // course over ground is noise for a vessel that is not moving
  return [
    { label: 'Speed', value: knots(ctx.fmt, p.sog), unit: p.sog != null ? 'kn' : '' },
    { label: 'Course', value: degrees(cog), unit: cog != null ? '°' : '' },
    { label: 'Heading', value: degrees(p.heading), unit: p.heading != null ? '°' : '' },
    { label: 'Draught', value: d.draught != null ? ctx.fmt.num(d.draught, 1) : null, unit: d.draught != null ? 'm' : '' },
  ];
}

function sizeText(ctx, d) {
  if (d.length && d.beam) return `${ctx.fmt.int(d.length)} × ${ctx.fmt.int(d.beam)} m`;
  if (d.length) return `${ctx.fmt.int(d.length)} m long`;
  return null;
}

function sections(ctx, d, trail) {
  const { fmt, h } = ctx;
  const out = [];
  out.push({ title: 'Vessel', rows: [
    ['MMSI', d.mmsi], ['IMO', d.imo], ['Call sign', d.callsign], ['Flag', d.flag_name ? `${d.flag_name} (from the MMSI)` : null],
    ['Type', d.type_label !== 'Type not reported' ? d.type_label : null], ['Size', sizeText(ctx, d)],
    ['AIS class', d.ais_class ? `Class ${d.ais_class}` : null],
  ] });
  const since = d.state && d.state !== 'underway' && d.state_since ? `, since ${fmt.utc(d.state_since)}` : '';
  const state = d.state ? `${stateLabel(d.state)}${since}` : null;
  out.push({ title: 'Voyage', rows: [
    ['Destination', d.destination], ['ETA', etaText(fmt, d.eta, d.eta_t, d.eta_past)],
    ['Draught', d.draught != null ? `${fmt.num(d.draught, 1)} m` : null], ['Status', d.nav_label],
    ['State', state && (since || stateLabel(d.state) !== d.nav_label) ? state : null],
  ] });
  if (d.position) {
    const p = d.position;
    out.push({ title: 'Position', rows: [
      ['Position', fmt.latlon(p.lat, p.lon, 4)], ['Last report', `${fmt.utc(p.t)} (${fmt.ago(p.t)})`],
      ['Rate of turn', p.rot != null && Math.abs(p.rot) >= 1 && Math.abs(p.rot) < 700
        ? `${fmt.int(Math.abs(p.rot))}° a minute to ${p.rot > 0 ? 'starboard' : 'port'}` : null],
      ['Source', (d.sources || []).join(', ') || d.source],
    ] });
  }
  if (d.sanctions) {
    const s = d.sanctions;
    const confirmed = d.sanctions_strength === 'confirmed';
    const matchText = s.match === 'imo' ? `IMO ${s.imo} (confirmed)` : s.match === 'mmsi' ? `MMSI ${s.mmsi} (confirmed)`
      : s.match === 'mmsi_conflict' ? 'MMSI only; the IMO differs, so not confirmed' : 'Name only, not confirmed';
    out.push({ title: confirmed ? 'Sanctions' : 'Possible sanctions match', rows: [
      ['Match', matchText], ['Listed as', s.name], ['Programmes', (s.programs || []).join(', ') || s.program],
      ['Aliases', (s.aliases || []).join(', ')], ['Linked to', (s.linked_to || []).join('; ')],
      ['Listed flag', s.flag], ['Listed type', s.vessel_type], ['Listed IMO', s.match !== 'imo' ? s.imo : null],
    ] });
    const lists = sanctionLists(s);
    out.push({ text: confirmed
      ? `This vessel is on ${listsSentence(lists)}.${lists.includes('OFAC') ? ' Dealings with it may be prohibited for US persons.' : ''}`
      : 'A name or MMSI match alone is weak: many vessels share names and MMSIs get reused. Check the IMO before relying on it.' });
  }
  if (d.dark) {
    const k = d.dark;
    out.push({ title: 'Dark ship estimate', rows: [
      ['Silent for', hours(k.silent_s)], ['Last report', `${fmt.utc(k.last.t)} at ${fmt.latlon(k.last.lat, k.last.lon, 3)}`],
      ['Last speed', k.last.sog != null ? `${fmt.num(k.last.sog, 1)} kn on ${degrees(k.last.cog) ?? '–'}°` : null],
      ['Estimated position', fmt.latlon(k.est.lat, k.est.lon, 3)], ['Uncertainty', `${fmt.num(k.radius_km, 0)} km radius`],
      ['Vessels still heard', `${k.neighbours_last} near its last position`],
    ] });
    out.push({ text: `Estimated by dead reckoning along its last course${k.capped ? ' for at most 3 hours' : ''}. Silence has innocent causes too: receiver gaps, a switched-off transponder in port approaches, or equipment faults.` });
  }
  if (d.suspect) {
    out.push({ title: 'Position suspect', text: `${d.suspect.sog ? `A reported speed of ${fmt.num(d.suspect.sog, 0)} kn is impossible for this vessel. ` : ''}${d.suspect.hotspot ? 'Several vessels report impossible positions in this spot, a typical sign of GNSS spoofing. ' : ''}The map shows what the vessel broadcast; its real position is unknown.` });
  }
  if (d.calls && d.calls.length) {
    out.push({ title: 'Recent stops', list: d.calls.map((c) => ({
      title: c.port || `Stop at ${fmt.latlon(c.lat, c.lon, 3)}`,
      subtitle: `${c.kind === 'anchored' ? 'At anchor' : 'Moored'} ${c.ongoing ? `since ${fmt.utc(c.arr_t)}` : `${fmt.utc(c.arr_t)} to ${fmt.clock(c.dep_t).slice(0, 5)} UTC (${hours(c.dep_t - c.arr_t)})`}${c.arrival_seen ? '' : ', arrival not seen'}`,
      detail: c.ongoing ? 'now' : fmt.ago(c.dep_t),
      onClick: () => ctx.map2.flyTo(c.lat, c.lon, 12),
    })) });
  }
  if (d.gaps && d.gaps.length) {
    out.push({ title: 'AIS gaps', list: d.gaps.map((g) => ({
      title: `Silent for ${fmt.num(g.hours, 1)} h`, subtitle: `${fmt.utc(g.t0)}${g.moved_km != null ? `, moved ${fmt.num(g.moved_km, 0)} km meanwhile` : ''}`,
      detail: fmt.ago(g.t1),
    })) });
  }
  const anomalies = vesselSection(ctx, d.anomalies, trail);
  if (anomalies) out.push(anomalies);
  const e = shipEntity(d);
  if (e.lat != null) out.push({ title: 'Nearby', nearby: { lat: e.lat, lon: e.lon, radius_km: 15, exclude: e, limit: 6 } });
  return out;
}

function foot(ctx, d) {
  if (!d.known) return 'Meridian keeps vessels for 7 days after their last report.';
  if (!d.position) return 'Static data only: no position report yet.';
  const src = d.source ? `Position from ${d.source}` : 'Position';
  return `${src}, ${ctx.fmt.ago(d.position.t)}. AIS is broadcast by the vessel itself and can be wrong or spoofed.`;
}

export function shipSpec(ctx, d, trail) {
  if (!d.known) {
    return {
      key: `ship:${d.mmsi}`, kind: 'ship', title: d.title, subtitle: d.flag_name ? `Flag ${d.flag_name}` : 'Vessel', icon: SHIP_ICON,
      accent: GROUP_COLOR, badges: [{ text: 'Not heard in 7 days', tone: 'grey' }],
      sections: [{ rows: [['MMSI', d.mmsi], ['Flag', d.flag_name]] }, { text: d.note }],
      links: d.links, entity: { kind: 'ship', id: String(d.mmsi), title: d.title, layer: 'ships' }, foot: foot(ctx, d),
    };
  }
  const cls = classOf(d.cls);
  return {
    key: `ship:${d.mmsi}`, kind: 'ship', title: d.title, subtitle: shipSubtitle(d), icon: SHIP_ICON, accent: GROUP_COLOR,
    badges: badges(ctx, d), block: block(ctx, d), sections: sections(ctx, d, trail), links: d.links,
    actions: [
      { text: trail.isOn(d.mmsi) ? 'Hide trail' : 'Show trail', primary: !trail.isOn(d.mmsi), onClick: async (card) => {
        await trail.toggle(d.mmsi, cls.color);
        card.update({ actions: shipSpec(ctx, d, trail).actions });
      } },
    ],
    entity: shipEntity(d), foot: foot(ctx, d),
    favPayload: { name: d.name, imo: d.imo },
  };
}

export async function openShip(ctx, entity, trail) {
  const mmsi = String(entity.id || '').trim();
  if (!/^\d{9}$/.test(mmsi)) {
    return ctx.cards.open({ key: `ship:${mmsi}`, kind: 'ship', title: entity.title || 'Vessel', icon: SHIP_ICON, accent: GROUP_COLOR,
      sections: [{ text: 'This vessel has no valid MMSI, so Meridian cannot look it up.' }] });
  }
  const placeholder = {
    key: `ship:${mmsi}`, kind: 'ship', title: entity.title || `MMSI ${mmsi}`, subtitle: entity.subtitle || 'Looking up the vessel…',
    icon: SHIP_ICON, accent: GROUP_COLOR,
    entity: { kind: 'ship', id: mmsi, title: entity.title || `MMSI ${mmsi}`, lat: entity.lat, lon: entity.lon, layer: 'ships' },
  };
  const card = ctx.cards.open(placeholder);
  let d;
  try {
    d = await ctx.api.get(`/api/ships/vessel/${mmsi}`);
  } catch (err) {
    card.update({ subtitle: 'Vessel details are unavailable right now.', badges: [{ text: 'Offline', tone: 'grey' }],
      sections: [{ text: err?.message || 'The ships module did not answer.' }] });
    return card;
  }
  if (!card.isOpen()) return card;
  const spec = shipSpec(ctx, d, trail);
  spec.live = d.known ? { everyMs: 30000, update: async () => {
    const next = await ctx.api.get(`/api/ships/vessel/${mmsi}`);
    const s = shipSpec(ctx, next, trail);
    return { title: s.title, subtitle: s.subtitle, badges: s.badges, block: s.block, sections: s.sections, entity: s.entity, foot: s.foot, actions: s.actions };
  } } : null;
  spec.onClose = () => { if (trail.isOn(mmsi)) trail.hide(mmsi); };
  return ctx.cards.open(spec);
}

// ------------------------------------------------------------------ strait card
function straitSpec(ctx, s) {
  const { fmt } = ctx;
  const byClass = Object.entries(s.unique_by_class || {}).sort((a, b) => b[1] - a[1]);
  const badgesList = [];
  if (!s.coverage) badgesList.push({ text: 'No live coverage', tone: 'grey' });
  if (s.congestion) badgesList.push({ text: `Busier than usual: ${s.congestion}× the 7-day median`, tone: 'amber' });
  const secs = [];
  if (s.coverage) {
    secs.push({ title: 'Inside now', rows: [['Moving', fmt.int(s.inside_moving)], ['Stationary', fmt.int(s.inside_stationary)]] });
    secs.push({ title: 'Transits in 24 hours', rows: [[s.pos_label, fmt.int(s.transits_pos)], [s.neg_label, fmt.int(s.transits_neg)],
      ['Unclear direction', fmt.int(Math.max(0, s.transits_24h - s.transits_pos - s.transits_neg))]] });
    if (byClass.length) secs.push({ title: 'Vessels in 24 hours by type', rows: byClass.map(([c, n]) => [classOf(c).label, fmt.int(n)]) });
    secs.push({ title: 'Vessels inside', list: (s.vessels || []).map(v => ({ title: v.title, subtitle: v.subtitle, entity: v })), empty: 'No vessels inside right now.' });
    secs.push({ text: `Live AIS from ${(s.sources || []).join(' and ') || 'AIS'}: ${fmt.int(s.heard_1h)} vessels heard around the strait in the last hour. A transit is a vessel moving within 50° of the main traffic direction inside the outline.` });
  } else {
    secs.push({ text: s.coverage_note || 'No AIS reports from this area in the last hour.' });
  }
  if (s.baseline_median != null) secs.push({ rows: [['Usual inside (7-day median)', fmt.num(s.baseline_median, 0)]] });
  if (s.portwatch) {
    secs.push({ title: 'PortWatch daily transits (satellite AIS)', el: portwatchSection(ctx, s.portwatch) });
    badgesList.push(...portwatchBadges(s.portwatch));
  }
  return {
    key: `strait:${s.id}`, kind: 'strait', title: s.name, subtitle: s.coverage ? 'Strait monitor' : 'Strait monitor, no live coverage',
    icon: STRAIT_ICON, accent: GROUP_COLOR, badges: badgesList,
    block: [
      { label: 'Inside now', value: s.coverage ? fmt.int(s.inside_now) : '–' },
      { label: 'Transits in 24 h', value: s.coverage ? fmt.int(s.transits_24h) : '–' },
      { label: 'Vessels in 24 h', value: s.coverage ? fmt.int(s.unique_24h) : '–' },
    ],
    sections: secs,
    entity: { kind: 'strait', id: `ships:${s.id}`, title: s.name, subtitle: 'Strait monitor', lat: s.label[1], lon: s.label[0], layer: 'ships.straits', bbox: s.bbox, zoom: 8 },
    foot: 'Counts come from AIS reports Meridian received; vessels without AIS or outside receiver range are missing.',
  };
}

export async function openStrait(ctx, entity) {
  if (String(entity.id || '').startsWith('portwatch:')) return openChokepoint(ctx, entity);
  const sid = String(entity.id || '').replace(/^ships:/, '');
  const card = ctx.cards.open({ key: `strait:${sid}`, kind: 'strait', title: entity.title || 'Strait', subtitle: 'Loading…', icon: STRAIT_ICON, accent: GROUP_COLOR });
  try {
    const s = await ctx.api.get(`/api/ships/straits/${encodeURIComponent(sid)}`);
    const spec = straitSpec(ctx, s);
    spec.live = { everyMs: 60000, update: async () => straitSpec(ctx, await ctx.api.get(`/api/ships/straits/${encodeURIComponent(sid)}`)) };
    return ctx.cards.open(spec);
  } catch (err) {
    card.update({ subtitle: err?.status === 404 ? 'Unknown strait' : 'The straits monitor is unavailable right now.', sections: [] });
    return card;
  }
}

// ------------------------------------------------------------------ port activity card
function portSpec(ctx, port, pa) {
  const { fmt } = ctx;
  const actions = [];
  if (port.source === 'infra' && ctx.moduleOk('infra')) {
    actions.push({ text: 'Port details', onClick: () => ctx.entities.open({ kind: 'port', id: port.id, title: port.name, lat: port.lat, lon: port.lon }, { fly: false }) });
  }
  return {
    key: `ships-port:${port.id}`, kind: 'port_activity', title: port.name, subtitle: `Port activity within ${fmt.km(pa.radius_km)}${port.country_name || port.country ? `, ${port.country_name || port.country}` : ''}`,
    icon: SHIP_ICON, accent: GROUP_COLOR, badges: pa.coverage_ok ? [] : [{ text: 'No live coverage', tone: 'grey' }],
    block: [
      { label: 'Moored', value: fmt.int(pa.moored) }, { label: 'At anchor', value: fmt.int(pa.anchored) },
      { label: 'Under way', value: fmt.int(pa.underway) }, { label: 'Arrivals 24 h', value: fmt.int(pa.arrivals_24h) },
    ],
    sections: [
      { rows: [['Departures in 24 hours', fmt.int(pa.departures_24h)]] },
      { title: 'Vessels', list: (pa.vessels || []).map(v => ({ title: v.title, subtitle: v.subtitle, detail: v.distance_km != null ? fmt.km(v.distance_km) : '', entity: v })), empty: 'No vessels reported in the port area.' },
      { text: pa.coverage },
      { text: pa.note },
    ],
    actions,
    entity: { kind: 'point', id: `${port.lat.toFixed(4)},${port.lon.toFixed(4)}`, title: port.name, lat: port.lat, lon: port.lon, zoom: 12 },
  };
}

export async function openPortActivity(ctx, port) {
  const radius = port.radius_km || 8;
  const card = ctx.cards.open({ key: `ships-port:${port.id}`, kind: 'port_activity', title: port.name, subtitle: 'Counting vessels…', icon: SHIP_ICON, accent: GROUP_COLOR });
  try {
    const pa = await ctx.api.get('/api/ships/port-activity', { lat: port.lat, lon: port.lon, radius_km: radius });
    const spec = portSpec(ctx, port, pa);
    spec.live = { everyMs: 60000, update: async () => portSpec(ctx, port, await ctx.api.get('/api/ships/port-activity', { lat: port.lat, lon: port.lon, radius_km: radius })) };
    return ctx.cards.open(spec);
  } catch {
    card.update({ subtitle: 'Port activity is unavailable right now.' });
    return card;
  }
}

// ------------------------------------------------------------------ SDN record (kind sanctioned_vessel)
export async function openSanctionRecord(ctx, entity, trail) {
  const uid = String(entity.id || '');
  const card = ctx.cards.open({ key: `sanctioned_vessel:${uid}`, kind: 'sanctioned_vessel', title: entity.title || 'Sanctioned vessel', subtitle: 'Loading the listing…', icon: SHIP_ICON, accent: GROUP_COLOR });
  let r;
  try {
    r = await ctx.api.get(`/api/ships/sdn/${encodeURIComponent(uid)}`);
  } catch (err) {
    card.update({ subtitle: err?.status === 404 ? 'Not on the current sanctions lists.' : 'The sanctions list is unavailable right now.' });
    return card;
  }
  if (r.live && r.live.mmsi) {
    card.close();
    return openShip(ctx, { kind: 'ship', id: r.live.mmsi, title: r.live.title, lat: r.live.lat, lon: r.live.lon }, trail);
  }
  const s = r.record;
  const lists = sanctionLists(s);
  const ofacOnly = lists.length === 1 && lists[0] === 'OFAC';
  return ctx.cards.open({
    key: `sanctioned_vessel:${uid}`, kind: 'sanctioned_vessel', title: s.name, subtitle: `Sanctioned vessel${s.vessel_type ? `, ${s.vessel_type}` : ''}`,
    icon: SHIP_ICON, accent: GROUP_COLOR, badges: [{ text: ofacOnly ? 'OFAC SDN list' : `Sanctioned (${lists.join(', ')})`, tone: 'red' }],
    block: [
      { label: 'IMO', value: s.imo || '–' },
      s.mmsi ? { label: 'MMSI', value: s.mmsi } : null,
      s.built ? { label: 'Built', value: String(s.built) } : null,
      { label: 'Programmes', value: ctx.fmt.int((s.programs || []).length) },
    ].filter(Boolean),
    sections: [
      { rows: [['Programmes', (s.programs || []).join(', ')], ['IMO', s.imo], ['MMSI', s.mmsi], ['Call sign', s.call_sign], ['Listed flag', s.flag],
        ['Aliases', (s.aliases || []).join(', ')], ['Linked to', (s.linked_to || []).join('; ')], ['Built', s.built]] },
      { text: r.note },
    ],
    links: r.links,
    // No position (the vessel is not being heard), but it can still be starred or added to a case file.
    entity: { kind: 'sanctioned_vessel', id: uid, title: s.name, subtitle: `Sanctioned vessel${s.imo ? `, IMO ${s.imo}` : ''}`, layer: 'ships.sanctioned' },
    foot: ofacOnly ? 'Listing from the US Treasury OFAC Specially Designated Nationals list.' : `Listing${lists.length > 1 ? 's' : ''} from ${listsSentence(lists)}.`,
  });
}
