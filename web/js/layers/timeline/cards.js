// Replay cards: an aircraft, vessel or event as it was at the replay time ("Position at 14:05 UTC").
// Open cards follow the playhead; "Open live card" opens the module's own card for the same thing
// without leaving replay, and "Show whole track" draws everything recorded for it.

import { AIR, SEA, positionAt, whenText, clockText, spanText, severityLevel } from './interp.js';
import { AIR_CLASSES, SEA_CLASSES, FLAG, GROUP_AIR, GROUP_SEA, airColor, seaColor } from './render.js';

const KEY = 'timeline:';
const KIND_LABELS = {
  earthquake: 'Earthquake', fire: 'Fire', volcano: 'Volcano', storm: 'Storm', flood: 'Flood', warning: 'Warning',
  outbreak: 'Outbreak', disaster: 'Disaster', tsunami: 'Tsunami', conflict_event: 'Conflict event', news: 'News',
  aircraft: 'Aircraft', ship: 'Vessel', gps_jam: 'GPS interference', cyber: 'Cyber', outage: 'Outage', strait: 'Strait',
  launch: 'Launch', satellite: 'Satellite', convergence: 'Convergence', risk_zone: 'Risk zone',
};

function niceDesc(text) {
  if (!text || /[a-z]/.test(text)) return text || null;
  return text.split(/(\s+)/).map(w => (/^[A-Z]{4,}$/.test(w) ? w[0] + w.slice(1).toLowerCase() : w)).join('');
}

export function createCards(ctx, { data, render, getT, getRange }) {
  const { fmt, api } = ctx;
  const open = new Map();          // card key -> {card, kind, id, static}
  let trackKey = null;
  let topKey = null;               // the card the highlight ring follows

  function liveCard(entity) {
    // The module's own card for the same thing, now. No fly and no layer switch: replay stays as it is.
    const { kind, id, title, subtitle } = entity;
    ctx.entities.open({ kind, id: String(id), title, subtitle }, { fly: false });
  }

  async function toggleTrack(key, kind, id, color) {
    const item = open.get(key);
    if (!item) return;
    if (trackKey === key) {
      trackKey = null;
      render.clearTrack();
      refresh(key);
      return;
    }
    const { start, end } = getRange();
    const path = kind === 'air' ? `/api/flights/history/${id}` : `/api/ships/history/${id}`;
    try {
      const r = await api.get(path, { from: Math.floor(start), to: Math.ceil(end) });
      if (!open.has(key)) return;
      trackKey = key;
      render.showTrack(r.positions || [], color);
      item.trackCount = r.count;
      if (!r.count) ctx.ui.toast('No recorded positions for the whole range.', 'amber');
    } catch (err) {
      ctx.ui.toast(`Could not load the track: ${err.message || err}`, 'red');
    }
    refresh(key);
  }

  function recorded(tr) {
    if (!tr || !tr.s.length) return null;
    return `${whenText(tr.s[0][0])} to ${whenText(tr.s[tr.s.length - 1][0])}, ${fmt.int(tr.s.length)} positions loaded`;
  }

  function sampleText(pos) {
    if (pos.state === 'exact') return `${clockText(pos.t0, true)} UTC (exact)`;
    if (pos.state === 'between') return `Between ${clockText(pos.t0, true)} and ${clockText(pos.t1, true)} UTC`;
    return `${clockText(pos.t0, true)} UTC, ${spanText(pos.age)} earlier`;
  }

  function aircraftSpec(hex, t) {
    const k = data.kinds.air;
    const tr = k.tracks.get(hex);
    const ident = k.idents.get(hex) || {};
    const pos = tr ? positionAt(tr, t, AIR, true) : null;
    const flags = pos ? pos.flags || 0 : (tr && tr.s.length ? tr.s[tr.s.length - 1][5] : 0);
    const cls = tr?.cls || 'unknown';
    const title = ident.cs || ident.reg || hex.toUpperCase();
    const subtitle = [niceDesc(ident.desc) || ident.typ, ident.reg].filter(Boolean).join(', ') || 'Aircraft';
    const badges = [{ text: 'Replay', tone: 'amber' }];
    if (flags & FLAG.EMG) badges.push({ text: 'Emergency', tone: 'red' });
    if (flags & FLAG.SANC) badges.push({ text: 'OFAC-listed', tone: 'red' });
    if (flags & FLAG.MIL) badges.push({ text: 'Military', tone: 'grey' });
    if (pos && pos.stale) badges.push({ text: 'Last known position', tone: 'grey' });
    const key = `${KEY}aircraft:${hex}`;
    const sections = [];
    let block = null;
    if (pos) {
      const onGround = !!(flags & FLAG.GND);
      block = [
        { label: 'Altitude', value: onGround ? 'Ground' : (pos.a == null ? '–' : fmt.int(Math.round(pos.a / 25) * 25)), unit: onGround || pos.a == null ? '' : 'ft' },
        { label: 'Speed', value: pos.speedKn == null || pos.speedKn > 1200 ? '–' : fmt.int(Math.round(pos.speedKn)), unit: pos.speedKn == null ? '' : 'kn' },
        { label: 'Track', value: pos.dir == null ? '–' : `${String(Math.round(pos.dir) % 360).padStart(3, '0')}°` },
      ];
      sections.push({ title: `Position at ${whenText(t)}`, rows: [
        ['Position', fmt.latlon(pos.lat, pos.lon)], ['Class', (AIR_CLASSES[cls] || AIR_CLASSES.unknown)[1]],
        ['Sample', sampleText(pos)], ['Recorded', recorded(tr)]] });
    } else {
      sections.push({ title: `Not seen at ${whenText(t)}`,
        text: tr && tr.s.length ? `Meridian recorded this aircraft here from ${recorded(tr)}.`
          : 'Meridian has no positions for this aircraft near this time in the area on screen.' });
    }
    sections.push({ title: 'About replay', text: 'Positions Meridian sampled at the time from the live feeds, at least a minute apart; '
      + 'between samples the aircraft is drawn on the great circle between them.' });
    return {
      key, kind: 'aircraft', title, subtitle, icon: '✈', accent: GROUP_AIR, badges, block, sections,
      actions: [{ text: 'Open live card', onClick: () => liveCard({ kind: 'aircraft', id: hex, title, subtitle }) },
        { text: trackKey === key ? 'Hide track' : 'Show whole track', onClick: () => toggleTrack(key, 'air', hex, airColor(cls, 0)) }],
      entity: pos ? { kind: 'aircraft', id: hex, title, subtitle, lat: +pos.lat.toFixed(5), lon: +pos.lon.toFixed(5), layer: 'flights' } : null,
      foot: `Replay of ${whenText(t, Date.now() / 1000, true)}`,
      _pos: pos, _color: airColor(cls, flags),
    };
  }

  function shipSpec(mmsi, t) {
    const k = data.kinds.sea;
    const tr = k.tracks.get(mmsi);
    const ident = k.idents.get(mmsi) || {};
    const pos = tr ? positionAt(tr, t, SEA, true) : null;
    const cls = ident.cls || tr?.cls || 'other';
    const title = ident.name || `MMSI ${mmsi}`;
    const subtitle = (SEA_CLASSES[cls] || SEA_CLASSES.other)[1];
    const badges = [{ text: 'Replay', tone: 'amber' }];
    if (pos && pos.stale) badges.push({ text: 'Last known position', tone: 'grey' });
    const key = `${KEY}ship:${mmsi}`;
    const sections = [];
    let block = null;
    if (pos) {
      const moving = pos.a != null && pos.a >= 1;
      block = [
        { label: 'Speed', value: pos.a == null ? '–' : fmt.num(pos.a, 1), unit: pos.a == null ? '' : 'kn' },
        { label: 'Course', value: moving && pos.dir != null ? `${String(Math.round(pos.dir) % 360).padStart(3, '0')}°` : '–' },
        { label: 'State', value: moving ? 'Under way' : 'Stationary' },
      ];
      sections.push({ title: `Position at ${whenText(t)}`, rows: [
        ['Position', fmt.latlon(pos.lat, pos.lon)], ['MMSI', String(mmsi)], ['Sample', sampleText(pos)], ['Recorded', recorded(tr)]] });
    } else {
      sections.push({ title: `Not seen at ${whenText(t)}`,
        text: tr && tr.s.length ? `Meridian recorded this vessel here from ${recorded(tr)}.`
          : 'Meridian has no positions for this vessel near this time in the area on screen.' });
    }
    sections.push({ title: 'About replay', text: 'The vessel track Meridian kept from AIS: a point every 2 to 10 minutes under way, '
      + 'every 2 hours when stationary, with straight lines between them.' });
    return {
      key, kind: 'ship', title, subtitle, icon: '⚓︎', accent: GROUP_SEA, badges, block, sections,
      actions: [{ text: 'Open live card', onClick: () => liveCard({ kind: 'ship', id: mmsi, title, subtitle }) },
        { text: trackKey === key ? 'Hide track' : 'Show whole track', onClick: () => toggleTrack(key, 'sea', mmsi, seaColor(cls)) }],
      entity: pos ? { kind: 'ship', id: String(mmsi), title, subtitle, lat: +pos.lat.toFixed(5), lon: +pos.lon.toFixed(5), layer: 'ships' } : null,
      foot: `Replay of ${whenText(t, Date.now() / 1000, true)}`,
      _pos: pos, _color: seaColor(cls),
    };
  }

  function eventSpec(evKey, t) {
    const ev = data.events.byKey.get(evKey);
    const key = `${KEY}event:${evKey}`;
    if (!ev) return { key, title: 'Event', sections: [{ text: 'This event is no longer loaded.' }] };
    const level = severityLevel(ev.sev);
    const tone = level === 'routine' ? 'grey' : level === 'notable' ? 'amber' : 'red';
    const span = ev.t1 - ev.t0;
    let when;
    if (t < ev.t0) when = `Starts ${spanText(ev.t0 - t)} after ${clockText(t)} UTC`;
    else if (t <= ev.t1 && span > 0) when = `Active at ${clockText(t)} UTC`;
    else when = `${spanText(t - ev.t1)} before ${clockText(t)} UTC`;
    const rows = [['When', span > 0 ? `${whenText(ev.t0)} to ${whenText(ev.t1)}` : whenText(ev.t1)],
      ['At the replay time', when], ['Kind', KIND_LABELS[ev.kind] || ev.kind], ['Reported by', ev.module],
      ['Position', fmt.latlon(ev.lat, ev.lon)]];
    const actions = [];
    if (ctx.entities.meta(ev.kind)) {
      actions.push({ text: 'Open live card', onClick: () => liveCard({ kind: ev.kind, id: ev.id, title: ev.title, subtitle: ev.subtitle }) });
    }
    return {
      key, kind: ev.kind, title: ev.title, subtitle: ev.subtitle, icon: '◎', accent: '#f0a43a',
      badges: [{ text: 'Replay', tone: 'amber' }, { text: `Severity ${fmt.num(ev.sev, ev.sev % 1 ? 1 : 0)}, ${level}`, tone }],
      block: [{ label: 'Severity', value: fmt.num(ev.sev, ev.sev % 1 ? 1 : 0), unit: '/10' },
        { label: span > 0 ? 'Active for' : 'Time', value: span > 0 ? spanText(span) : clockText(ev.t1), unit: span > 0 ? '' : 'UTC' }],
      sections: [{ title: 'Event', rows }],
      actions,
      entity: { kind: ev.kind, id: ev.id, title: ev.title, subtitle: ev.subtitle, lat: ev.lat, lon: ev.lon },
      foot: 'From Meridian\'s event history (a snapshot every 5 minutes).',
      _pos: { lat: ev.lat, lon: ev.lon }, _color: '#f0a43a', _static: true,
    };
  }

  function specFor(item, t) {
    if (item.kind === 'air') return aircraftSpec(item.id, t);
    if (item.kind === 'sea') return shipSpec(item.id, t);
    return eventSpec(item.id, t);
  }

  function show(kind, id) {
    const t = getT();
    const item = { kind, id: String(id), card: null };
    const spec = specFor(item, t);
    const { _pos, _color, _static, ...clean } = spec;
    if (!clean.entity && _pos) ctx.map2.highlight(_pos.lat, _pos.lon, _color);
    const card = ctx.cards.open({ ...clean, onClose: () => {
      open.delete(spec.key);
      if (topKey === spec.key) topKey = null;
      if (trackKey === spec.key) { trackKey = null; render.clearTrack(); }
    } });
    item.card = card;
    item.static = !!_static;
    open.set(spec.key, item);
    topKey = spec.key;
    return card;
  }

  function refresh(key, t = getT()) {
    const item = open.get(key);
    if (!item || !item.card.isOpen()) { open.delete(key); return; }
    const { _pos, _color, _static, ...clean } = specFor(item, t);
    item.card.update(clean);
    if (_pos && !_static && key === topKey) ctx.map2.highlight(_pos.lat, _pos.lon, _color);
  }

  /** Follow the playhead (called a few times a second while replaying). */
  function update(t) {
    for (const key of [...open.keys()]) refresh(key, t);
  }

  function closeAll() {
    for (const item of [...open.values()]) item.card.close();
    open.clear();
    trackKey = null;
  }

  return { show, update, closeAll, count: () => open.size };
}
