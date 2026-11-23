// Replay data: prefetch time chunks of sampled aircraft and vessel positions around the playhead
// for the area on screen (padded), merge them into one sorted track per aircraft or vessel, and
// evict chunks far from the playhead. Events for the whole range come in one request per area.
//
// Endpoints: /api/flights/history and /api/ships/history (CONTRACT 15.4, format=rows) and
// /api/core/history/events (CONTRACT 15.3).

import { chunksFor, padBox, boxContains } from './interp.js';

export const KINDS = {
  // chunk: seconds per request; before/after: what must be loaded around t to draw it (the
  // previous sample and trail behind, the next sample ahead); keep: chunks held in memory.
  air: { path: '/api/flights/history', chunk: 900, before: 17 * 60, after: 7 * 60, keep: 32, idKey: 'hex' },
  sea: { path: '/api/ships/history', chunk: 1800, before: 2.25 * 3600, after: 35 * 60, keep: 24, idKey: 'mmsi' },
};
const LIMIT = 50000;
const MAX_INFLIGHT = 3;
const EVENTS_LIMIT = 10000;
const EVENTS_TTL_MS = 5 * 60 * 1000;

function freshKind(name) {
  return { name, box: null, chunks: new Map(), tracks: new Map(), idents: new Map(), inflight: new Map(),
    truncated: 0, errors: 0, gen: 0, available: null };
}

export function createData(ctx, { onData }) {
  const { api } = ctx;
  const kinds = { air: freshKind('air'), sea: freshKind('sea') };
  const events = { box: null, from: 0, to: 0, list: [], byKey: new Map(), loading: false, truncated: false,
    error: null, at: 0, ctrl: null };
  const enabled = { air: true, sea: true, events: true };
  let lastT = null;
  let lastSpeed = 0;

  function reset(k) {
    for (const c of k.inflight.values()) c.abort();
    k.inflight.clear();
    k.chunks.clear();
    k.tracks.clear();
    k.idents.clear();
    k.truncated = 0;
    k.errors = 0;
    k.gen += 1;
  }

  /** The view changed: keep the cache while the view stays inside the loaded box. */
  function setView(view) {
    if (!view) return;
    for (const k of Object.values(kinds)) {
      if (k.box && boxContains(k.box, view) && !(k.truncated && area(k.box) > 6 * area(view))) continue;
      reset(k);
      k.box = padBox(view, 0.3);
    }
    if (!events.box || !boxContains(events.box, view)) {
      events.box = padBox(view, 0.5);
      events.at = 0;
    }
  }

  function area(b) {
    const w = b[2] >= b[0] ? b[2] - b[0] : b[2] + 360 - b[0];
    return Math.max(1e-6, w * (b[3] - b[1]));
  }

  /** Make sure what is needed to draw time t is loaded or loading; prefetch ahead at speed. */
  function ensure(t, speed = 0) {
    lastT = t;
    lastSpeed = speed;
    for (const [name, k] of Object.entries(kinds)) {
      if (!enabled[name] || !k.box) continue;
      const spec = KINDS[name];
      const ahead = spec.after + Math.max(0, speed) * 4;     // four seconds of playback
      const behind = spec.before + Math.max(0, -speed) * 4;
      const wanted = chunksFor(t - behind, t + ahead, spec.chunk)
        .sort((a, b) => Math.abs(a + spec.chunk / 2 - t) - Math.abs(b + spec.chunk / 2 - t));
      for (const c0 of wanted) {
        if (k.inflight.size >= MAX_INFLIGHT) break;
        if (k.chunks.has(c0) || k.inflight.has(c0)) continue;
        if (c0 > Date.now() / 1000) continue;
        load(name, k, c0);
      }
      evict(name, k, t);
    }
  }

  async function load(name, k, c0) {
    const spec = KINDS[name];
    const ctrl = new AbortController();
    const gen = k.gen;
    k.inflight.set(c0, ctrl);
    const started = performance.now();
    try {
      const r = await api.get(spec.path, { from: c0, to: c0 + spec.chunk - 1, bbox: k.box.join(','), limit: LIMIT,
        format: 'rows' }, { signal: ctrl.signal });
      if (gen !== k.gen) return;
      merge(name, k, r);
      k.available = r.available || k.available;
      k.chunks.set(c0, { at: Date.now(), count: r.count, truncated: !!r.truncated, ms: Math.round(performance.now() - started),
        bytes: null, live: c0 + spec.chunk > Date.now() / 1000 - 120 });
      if (r.truncated) k.truncated += 1;
    } catch (err) {
      if (err?.name === 'AbortError' || gen !== k.gen) return;
      k.errors += 1;
      k.chunks.set(c0, { at: Date.now(), count: 0, error: err?.message || String(err), retry: Date.now() + 15000 });
    } finally {
      if (k.inflight.get(c0) === ctrl) k.inflight.delete(c0);
    }
    onData?.(name);
    if (lastT != null) ensure(lastT, lastSpeed);
  }

  function merge(name, k, r) {
    const rows = r.rows || [];
    const dirty = new Set();
    if (name === 'air') {
      for (const row of rows) {
        const [hex, t, lat, lon, alt, trk, cls, flags] = row;
        let tr = k.tracks.get(hex);
        if (!tr) { tr = { id: hex, kind: 'air', cls, s: [] }; k.tracks.set(hex, tr); }
        tr.cls = cls;
        tr.s.push([t, lat, lon, alt, trk, flags]);
        dirty.add(tr);
      }
      for (const [hex, ident] of Object.entries(r.idents || {})) k.idents.set(hex, ident);
    } else {
      for (const row of rows) {
        const [mmsi, t, lat, lon, sog, cog, cls] = row;
        let tr = k.tracks.get(mmsi);
        if (!tr) { tr = { id: mmsi, kind: 'sea', cls, s: [] }; k.tracks.set(mmsi, tr); }
        tr.cls = cls;
        tr.s.push([t, lat, lon, sog, cog, 0]);
        dirty.add(tr);
      }
      for (const [mmsi, ident] of Object.entries(r.idents || {})) k.idents.set(mmsi, ident);
    }
    for (const tr of dirty) tidy(tr);
  }

  function tidy(tr) {
    tr.s.sort((a, b) => a[0] - b[0]);
    let w = 0;
    for (let i = 0; i < tr.s.length; i++) {
      if (w && tr.s[w - 1][0] === tr.s[i][0]) { tr.s[w - 1] = tr.s[i]; continue; }
      tr.s[w++] = tr.s[i];
    }
    tr.s.length = w;
  }

  function evict(name, k, t) {
    const spec = KINDS[name];
    // Failed chunks may be retried after a pause; chunks touching the present are refreshed.
    const now = Date.now();
    for (const [c0, c] of k.chunks) {
      if ((c.error && now > c.retry) || (c.live && now - c.at > 60000)) k.chunks.delete(c0);
    }
    if (k.chunks.size <= spec.keep) return;
    const far = [...k.chunks.keys()].sort((a, b) => Math.abs(b - t) - Math.abs(a - t));
    const drop = far.slice(0, k.chunks.size - spec.keep);
    for (const c0 of drop) k.chunks.delete(c0);
    const spans = drop.map(c0 => [c0, c0 + spec.chunk]);
    for (const [id, tr] of k.tracks) {
      tr.s = tr.s.filter(p => !spans.some(([a, b]) => p[0] >= a && p[0] < b));
      if (!tr.s.length) k.tracks.delete(id);
    }
  }

  /** Events active in [from, to] in the padded view (one request, refreshed every 5 minutes). */
  async function loadEvents(from, to, { force = false } = {}) {
    if (!enabled.events || !events.box) return;
    if (!force && events.at && Date.now() - events.at < EVENTS_TTL_MS && events.from <= from && events.to >= to - 120) return;
    events.ctrl?.abort();
    const ctrl = new AbortController();
    events.ctrl = ctrl;
    events.loading = true;
    try {
      const r = await api.get('/api/core/history/events', { from: Math.floor(from), to: Math.ceil(to),
        bbox: events.box.join(','), limit: EVENTS_LIMIT }, { signal: ctrl.signal });
      if (events.ctrl !== ctrl) return;
      const list = [];
      const byKey = new Map();
      for (const e of r.events || []) {
        const ev = { key: `${e.module}:${e.kind}:${e.id}`, id: String(e.id), kind: e.kind, module: e.module, title: e.title || e.kind,
          subtitle: e.subtitle || '', lat: +e.lat, lon: +e.lon, t0: +(e.first_time ?? e.time), t1: +e.time, sev: +e.severity || 0,
          layer: e.layer || null, raw: e };
        list.push(ev);
        byKey.set(ev.key, ev);
      }
      events.list = list;
      events.byKey = byKey;
      events.truncated = !!r.truncated;
      events.from = from;
      events.to = to;
      events.at = Date.now();
      events.error = null;
    } catch (err) {
      if (err?.name === 'AbortError') return;
      events.error = err?.message || String(err);
      events.at = Date.now() - EVENTS_TTL_MS + 30000;     // try again in 30 s
    } finally {
      if (events.ctrl === ctrl) { events.loading = false; events.ctrl = null; }
    }
    onData?.('events');
  }

  function setEnabled(name, on) {
    enabled[name] = !!on;
    if (!on && kinds[name]) reset(kinds[name]);
  }

  function clear() {
    for (const k of Object.values(kinds)) { reset(k); k.box = null; }
    events.ctrl?.abort();
    events.list = [];
    events.byKey = new Map();
    events.box = null;
    events.at = 0;
    lastT = null;
  }

  function loading() {
    return kinds.air.inflight.size + kinds.sea.inflight.size + (events.loading ? 1 : 0);
  }

  /** Is everything needed to draw t loaded (for the "Loading" note)? */
  function ready(t) {
    for (const [name, k] of Object.entries(kinds)) {
      if (!enabled[name] || !k.box) continue;
      const spec = KINDS[name];
      for (const c0 of chunksFor(t - spec.before, t + spec.after, spec.chunk)) {
        if (c0 > Date.now() / 1000) continue;
        if (!k.chunks.has(c0)) return false;
      }
    }
    return true;
  }

  function stats() {
    const one = (k) => ({ tracks: k.tracks.size, chunks: k.chunks.size, inflight: k.inflight.size, truncated: k.truncated,
      errors: k.errors, samples: [...k.tracks.values()].reduce((n, tr) => n + tr.s.length, 0),
      ms: [...k.chunks.values()].map(c => c.ms).filter(Number.isFinite) });
    return { air: one(kinds.air), sea: one(kinds.sea), events: events.list.length, eventsTruncated: events.truncated };
  }

  return { kinds, events, enabled, setView, ensure, loadEvents, setEnabled, clear, loading, ready, stats };
}
