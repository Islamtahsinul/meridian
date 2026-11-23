// Timeline replay (browser half of the timeline module). A "Timeline" toolbar button and keyboard
// shortcut open a time bar docked above the status bar. Dragging the playhead, playing or stepping
// puts Meridian into replay (ctx.time): the live aircraft and vessel layers are switched off and
// replay layers draw aircraft and vessels where they were at that time, with short trails, plus
// the events active around it. "Back to live" or closing the bar restores every layer as it was.
//
// Nothing runs until the bar is opened: no requests, no map sources, no timers.
// Parts: ./timeline/bar.js (the bar), data.js (prefetch and tracks), render.js (map layers),
// cards.js (replay cards), interp.js (maths).

import { createBar } from './timeline/bar.js';
import { createData } from './timeline/data.js';
import { createRender, CLICKABLE, MAX_AIR, MAX_SEA } from './timeline/render.js';
import { createCards } from './timeline/cards.js';
import { whenText, clockText, severityLevel } from './timeline/interp.js';

const LIVE_LAYERS = ['flights', 'flights.military', 'flights.alerts', 'ships', 'ships.dark', 'ships.sanctioned'];
const RANGE_S = 72 * 3600;
const ICON = 'M10 3.2a6.8 6.8 0 1 1-6.5 4.8M10 6.2V10l2.6 1.7M2.6 4.2v3.6h3.6';
const FRAME_MS = 90;            // map redraws at most ~11 times a second
const CARD_MS = 250;
const SEA_MS = 320;             // vessels redraw about three times a second during playback
const RANGE_REFRESH_MS = 5 * 60 * 1000;
const SHORTCUT_KEYS = ['r', 'h', 'y', 'k'];
const SAVED_LAYERS = 'meridian.layers.on';

export async function register(ctx) {
  const { ui, bus } = ctx;
  const st = {
    open: false, replay: false, t: null, playing: false, speed: 300, start: Date.now() / 1000 - RANGE_S,
    end: Date.now() / 1000, hidden: [], self: false, lastFrame: 0, lastCard: 0, raf: 0, playTs: 0, dirty: false,
    rangeAt: 0, rangeTimer: null, tickTimer: null, toasted: false, coverage: { air: [], sea: [] }, hours: [],
    available: { air: null, sea: null }, hasEvents: false, frameCost: 0, lastSea: 0,
  };
  let bar = null;
  let data = null;
  let render = null;
  let cards = null;
  let btn = null;

  // -------------------------------------------------------------- clock helpers
  const nowS = () => Date.now() / 1000;
  function selfTime(fn) {
    st.self = true;
    try { return fn(); } finally { st.self = false; }
  }

  // -------------------------------------------------------------- open and close
  function ensureParts() {
    if (bar) return;
    data = createData(ctx, { onData: () => { st.dirty = true; schedule(); } });
    render = createRender(ctx, data);
    cards = createCards(ctx, { data, render, getT: () => st.t ?? nowS(), getRange: () => ({ start: st.start, end: st.end }) });
    bar = createBar(ctx, {
      seek: (t, { final }) => seek(t, { final }),
      toggle: () => (st.playing ? pause() : playNow()),
      speed: (v) => setSpeed(v),
      step: (dt) => seek((st.t ?? st.end) + dt, { final: true }),
      live: () => backToLive(),
      close: () => close(),
    });
    bar.setSpeed(st.speed);
    data.setEnabled('air', ctx.moduleOk('flights'));
    data.setEnabled('sea', ctx.moduleOk('ships'));
    ctx.map2.onClick(CLICKABLE, (f) => {
      if (!st.replay) return;
      const p = f.properties || {};
      const layer = f.layer?.id || '';
      if (layer.startsWith('ov-timeline-air')) cards.show('air', p.id);
      else if (layer.startsWith('ov-timeline-sea')) cards.show('sea', String(p.id));
      else if (layer === 'ov-timeline-events-dot') cards.show('event', p.key);
    });
    ctx.map2.onHover(CLICKABLE, (f) => hoverText(f));
  }

  function open() {
    if (st.open) return;
    ensureParts();
    st.open = true;
    document.getElementById('app').classList.add('timeline-open');
    document.getElementById('app').append(bar.el);
    btn?.setAttribute('aria-pressed', 'true');
    st.end = nowS();
    bar.setRange(st.start, st.end);
    bar.setTime(null, 'live');
    bar.setNote('Drag the playhead or press play to replay.');
    resizeMap();
    refreshRange();
    st.rangeTimer = setInterval(() => { if (document.visibilityState === 'visible') refreshRange(); }, RANGE_REFRESH_MS);
    st.tickTimer = setInterval(tick, 15000);
    requestAnimationFrame(() => bar.focus());
  }

  async function close() {
    if (!st.open) return;
    if (st.replay) await exitReplay();
    st.open = false;
    clearInterval(st.rangeTimer);
    clearInterval(st.tickTimer);
    bar.el.remove();
    document.getElementById('app').classList.remove('timeline-open');
    btn?.setAttribute('aria-pressed', 'false');
    resizeMap();
  }

  function resizeMap() {
    requestAnimationFrame(() => { try { ctx.map.resize(); } catch { /* map not ready */ } });
  }

  function tick() {
    // Keep the end of the range at "now" and the live readout current.
    st.end = nowS();
    if (bar) {
      bar.setRange(st.start, st.end);
      bar.setTime(st.t, st.replay ? 'replay' : 'live');
    }
    if (st.replay) data.loadEvents(st.start, st.end);    // new events, at most every 5 minutes
  }

  // -------------------------------------------------------------- range, histogram and coverage
  async function refreshRange({ coverage = true } = {}) {
    const now = nowS();
    const from = Math.floor(now - RANGE_S);
    const view = ctx.map2.viewBounds();
    const cov = (mod, path) => (coverage && ctx.moduleOk(mod) ? ctx.api.get(path, { from, to: Math.ceil(now) }).catch(() => null) : null);
    const [summary, air, sea] = await Promise.all([
      ctx.api.get('/api/core/history/summary', { from, to: Math.ceil(now), bbox: view.join(',') }).catch(() => null),
      cov('flights', '/api/flights/history/coverage'), cov('ships', '/api/ships/history/coverage')]);
    if (!st.open) return;
    st.hours = summary?.by_hour || [];
    if (coverage) {
      st.coverage = { air: air?.by_hour || [], sea: sea?.by_hour || [] };
      st.available = { air: air?.available || null, sea: sea?.available || null };
    }
    const firsts = [st.hours[0]?.t, st.coverage.air[0]?.t, st.coverage.sea[0]?.t, st.available.air?.from, st.available.sea?.from]
      .filter(v => Number.isFinite(v) && v >= from - 3600);
    const anything = firsts.length > 0;
    st.start = anything ? Math.max(from, Math.floor(Math.min(...firsts) / 3600) * 3600) : now - 3600;
    st.end = now;
    st.rangeAt = Date.now();
    bar.setRange(st.start, st.end);
    bar.setData({ hours: st.hours, air: st.coverage.air, sea: st.coverage.sea });
    bar.setEmpty(anything ? '' : 'History starts when Meridian has been running for a while.');
    if (st.t != null && st.t < st.start) seek(st.start, { final: true });
    bar.setTime(st.t, st.replay ? 'replay' : 'live');
  }

  // -------------------------------------------------------------- entering and leaving replay
  async function enterReplay(t, { adopt = false } = {}) {
    if (st.replay) return;
    st.replay = true;
    st.t = clampT(t);
    if (!adopt) selfTime(() => ctx.time.setReplay(Math.round(st.t * 1000)));
    st.hidden = LIVE_LAYERS.filter(id => ctx.layers.isOn(id));
    try { await ctx.layers.suspend(st.hidden); } catch (err) { console.warn('timeline: could not hide live layers', err); }
    render.add();
    data.setView(ctx.map2.viewBounds());
    data.loadEvents(st.start, st.end);
    st.dirty = true;
    bar.setTime(st.t, 'replay');
    schedule();
  }

  async function exitReplay({ adopt = false } = {}) {
    if (!st.replay) return;
    st.replay = false;               // first: pause() must not move the clock back into replay
    pause();
    st.t = null;
    cards.closeAll();
    render.remove();
    data.clear();
    if (!adopt) selfTime(() => ctx.time.live());
    const restore = st.hidden;
    st.hidden = [];
    try { await ctx.layers.resume(restore); } catch (err) { console.warn('timeline: could not restore live layers', err); }
    if (bar) {
      bar.setTime(null, 'live');
      bar.setNote('Back to live. Drag the playhead or press play to replay.');
    }
  }

  async function backToLive() {
    await exitReplay();
  }

  function clampT(t) {
    const now = nowS();
    return Math.max(Math.min(st.start, now - 60), Math.min(now, +t));
  }

  // -------------------------------------------------------------- moving the playhead
  async function seek(t, { final = true } = {}) {
    if (!st.open) open();
    const target = clampT(t);
    if (!st.replay) { await enterReplay(target); return; }
    st.t = target;
    selfTimeThrottled(final);
    bar.setTime(st.t, 'replay');
    st.dirty = true;
    schedule(final);
  }

  let lastClockMs = 0;
  function selfTimeThrottled(force) {
    const now = performance.now();
    if (!force && now - lastClockMs < FRAME_MS) return;
    lastClockMs = now;
    selfTime(() => ctx.time.setReplay(Math.round(st.t * 1000)));
  }

  function setSpeed(v) {
    st.speed = v;
    bar.setSpeed(v);
    if (!st.playing) playNow();
  }

  async function playNow() {
    if (!st.open) open();
    if (!st.replay) {
      // Playing from live starts an hour back (or at the start of history).
      await enterReplay(Math.max(st.start, nowS() - 3600));
    } else if (st.t >= nowS() - 5) {
      st.t = Math.max(st.start, nowS() - 3600);
    }
    st.playing = true;
    st.playTs = performance.now();
    bar.setPlaying(true);
    loop();
  }

  function pause() {
    st.playing = false;
    if (bar) bar.setPlaying(false);
    if (st.replay) selfTimeThrottled(true);
  }

  function loop() {
    cancelAnimationFrame(st.raf);
    st.raf = requestAnimationFrame((ts) => {
      if (!st.playing || !st.replay) return;
      const dt = Math.min(0.25, (ts - st.playTs) / 1000);
      st.playTs = ts;
      // Hold the playhead while the data it needs is still arriving (a stall, not a jump).
      if (data.ready(st.t) || data.loading() === 0) {
        let t = st.t + dt * st.speed;
        const now = nowS();
        if (t >= now) {
          t = now;
          st.t = t;
          pause();
          bar.setNote('Caught up with the present. Press Back to live to leave replay.');
        }
        st.t = t;
        selfTimeThrottled(false);
        bar.setTime(st.t, 'replay');
        st.dirty = true;
      }
      frame(ts);
      if (st.playing) loop();
    });
  }

  // -------------------------------------------------------------- drawing
  let scheduled = false;
  function schedule(force = false) {
    if (force) st.lastFrame = 0;
    if (scheduled || st.playing) return;
    scheduled = true;
    requestAnimationFrame((ts) => {
      scheduled = false;
      frame(ts, true);
      if (st.dirty && !st.playing) schedule();
    });
  }

  function frame(ts, idle = false) {
    if (!st.replay || st.t == null) return;
    // At most ~11 redraws a second, fewer when a redraw is expensive (keeps the page responsive).
    if (ts - st.lastFrame < Math.max(FRAME_MS, st.frameCost * 2.5)) return;
    if (!st.dirty) return;
    st.dirty = false;
    st.lastFrame = ts;
    data.ensure(st.t, st.playing ? st.speed : 0);
    const sea = !st.playing || idle || ts - st.lastSea >= SEA_MS;
    if (sea) st.lastSea = ts;
    st.frameCost = render.frame(st.t, ctx.map.getZoom(), { sea });
    if (ts - st.lastCard >= CARD_MS || idle) {
      st.lastCard = ts;
      cards.update(st.t);
      status();
    }
  }

  function status() {
    const c = render.counts();
    const loading = data.loading();
    const bits = [];
    if (data.enabled.air) bits.push(`${ctx.fmt.int(c.air)} aircraft`);
    if (data.enabled.sea) bits.push(`${ctx.fmt.int(c.sea)} vessels`);
    if (data.enabled.events) bits.push(`${ctx.fmt.int(c.events)} events`);
    let text = bits.join(', ');
    let tone = '';
    if (loading) { text = `Loading, ${text}`; tone = 'busy'; }
    else if (!c.air && !c.sea && !c.events) { text = 'Nothing recorded here at this time.'; }
    if (c.airCapped || c.seaCapped || data.kinds.air.truncated || data.kinds.sea.truncated) {
      text += '. Showing part of the traffic: zoom in for all of it';
      tone = 'warn';
    }
    bar.setNote(text, tone);
  }

  function hoverText(f) {
    if (!st.replay) return null;
    const p = f.properties || {};
    const layer = f.layer?.id || '';
    if (layer.startsWith('ov-timeline-air')) {
      const ident = data.kinds.air.idents.get(p.id) || {};
      const pos = render.visible.air.get(p.id);
      const alt = pos && pos.a != null && !(pos.flags & 16) ? `, ${ctx.fmt.int(Math.round(pos.a / 100) * 100)} ft` : (pos && pos.flags & 16 ? ', on the ground' : '');
      return `${ident.cs || ident.reg || String(p.id).toUpperCase()}${alt} at ${clockText(st.t)} UTC`;
    }
    if (layer.startsWith('ov-timeline-sea')) {
      const ident = data.kinds.sea.idents.get(String(p.id)) || {};
      const pos = render.visible.sea.get(String(p.id));
      const speed = pos && pos.a != null ? `, ${ctx.fmt.num(pos.a, 1)} kn` : '';
      return `${ident.name || `MMSI ${p.id}`}${speed} at ${clockText(st.t)} UTC`;
    }
    const ev = data.events.byKey.get(p.key);
    return ev ? `${ev.title} (${severityLevel(ev.sev)}, ${whenText(ev.t1)})` : null;
  }

  // -------------------------------------------------------------- the rest of the app
  bus.on('map:moveend', () => {
    if (!st.replay) return;
    data.setView(ctx.map2.viewBounds());
    data.loadEvents(st.start, st.end);
    st.dirty = true;
    schedule(true);
  });
  let rangeMove = null;
  bus.on('map:moveend', () => {
    if (!st.open) return;
    clearTimeout(rangeMove);
    rangeMove = setTimeout(() => refreshRange({ coverage: false }), 800);   // the histogram counts events in view
  });
  bus.on('layer:toggled', ({ id, on }) => {
    if (!st.replay || !on || !st.hidden.includes(id)) return;
    st.hidden = st.hidden.filter(x => x !== id);    // the user wants it back: leave it on at exit too
    if (!st.toasted) { st.toasted = true; ui.toast('Live layers show current positions on top of the replay.', 'amber', 5000); }
  });
  bus.on('time:mode', ({ mode }) => {
    if (st.self) return;
    if (mode === 'live' && st.replay) exitReplay({ adopt: true });
    else if (mode === 'replay' && !st.replay) { open(); enterReplay(ctx.time.now() / 1000, { adopt: true }); }
  });
  bus.on('time:changed', ({ t, mode }) => {
    if (st.self || mode !== 'replay' || !st.replay || t == null) return;
    if (Math.abs(t / 1000 - st.t) >= 1) { st.t = t / 1000; bar.setTime(st.t, 'replay'); st.dirty = true; schedule(true); }
  });
  // A reload is live (CONTRACT 15.3): put the layers replay switched off back into the saved set
  // first, so they come back even if another layer was toggled meanwhile.
  window.addEventListener('pagehide', () => {
    if (!st.replay || !st.hidden.length) return;
    try {
      const saved = new Set(JSON.parse(localStorage.getItem(SAVED_LAYERS) || '[]'));
      for (const id of st.hidden) saved.add(id);
      localStorage.setItem(SAVED_LAYERS, JSON.stringify([...saved]));
    } catch { /* storage unavailable */ }
  });

  const taken = new Set(ui.listShortcuts().map(s => s.key));
  const key = SHORTCUT_KEYS.find(k => !taken.has(k));
  const toggleBar = () => (st.open ? close() : open());
  btn = ui.toolbarButton({ id: 'timeline', title: `Timeline: replay the last 72 hours${key ? ` (${key.toUpperCase()})` : ''}`,
    label: 'Timeline', icon: ICON, order: 12, onClick: toggleBar });
  if (key) ui.shortcut(key, 'Timeline: replay the last 72 hours', toggleBar);

  // A map action for the assistant and other modules: {type: 'replay_at', time, lat?, lon?, zoom?}.
  ctx.ai.register('replay_at', async ({ time, lat, lon, zoom }) => {
    const ms = ctx.dom.toMs(time);
    if (!Number.isFinite(ms)) throw new Error('replay_at needs a time');
    if (ctx.dom.validLatLon(lat, lon)) ctx.map2.flyTo(+lat, +lon, zoom != null ? +zoom : undefined);
    open();
    await seek(ms / 1000, { final: true });
  });

  // Debugging and test handle (tools/browser_check.mjs --eval).
  ctx.timeline = {
    open, close, seek: (t) => seek(ctx.dom.toMs(t) / 1000, { final: true }), play: (speed) => (speed ? setSpeed(speed) : playNow()),
    pause, live: backToLive, state: () => ({ open: st.open, replay: st.replay, t: st.t, playing: st.playing, speed: st.speed,
      start: st.start, end: st.end, hidden: [...st.hidden], key, hours: st.hours.length,
      coverage: { air: st.coverage.air.length, sea: st.coverage.sea.length } }),
    stats: () => ({ data: data?.stats(), render: render?.counts(), cards: cards?.count() ?? 0, max: { air: MAX_AIR, sea: MAX_SEA } }),
    openCard: (kind, id) => cards?.show(kind, id),
    internals: () => ({ data, render }),
    visible: () => ({ air: render ? [...render.visible.air.keys()] : [], sea: render ? [...render.visible.sea.keys()] : [],
      events: data ? data.events.list.length : 0 }),
  };
}
