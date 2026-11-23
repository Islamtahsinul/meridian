// The time bar, docked above the status bar while the timeline is open: the last 72 hours (or since
// history began) with an event-density histogram by hour coloured by the highest severity,
// aircraft and ship coverage strips, a draggable playhead, play and pause with four speeds, steps
// of an hour, the UTC time and "Back to live". Pure DOM; the controller (../timeline.js) owns state.

import { ticks, clockText, dateText, whenText, severityLevel } from './interp.js';
import { SEVERITY, GROUP_AIR, GROUP_SEA } from './render.js';

export const SPEEDS = [
  { v: 60, label: '1 min/s', title: 'One minute per second' },
  { v: 300, label: '5 min/s', title: 'Five minutes per second' },
  { v: 900, label: '15 min/s', title: 'Fifteen minutes per second' },
  { v: 3600, label: '1 h/s', title: 'One hour per second' },
];

const SVGNS = 'http://www.w3.org/2000/svg';
const HIST_H = 26;          // histogram band (px)
const STRIP_Y = [HIST_H + 7, HIST_H + 17];   // centre lines of the aircraft and ships strips
const TRACK_H = HIST_H + 22;
const ICON_PLAY = 'M6.5 4.5v11l9-5.5z';
const ICON_PAUSE = 'M6 4.5h2.6v11H6zM11.4 4.5H14v11h-2.6z';
const ICON_CLOSE = 'M5 5l10 10M15 5 5 15';

function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

/** A column path from the baseline with rounded top corners. */
function column(x, w, hgt, base, r) {
  const y = base - hgt;
  const rr = Math.max(0, Math.min(r, w / 2, hgt));
  return `M${x},${base}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${base}Z`;
}

export function createBar(ctx, on) {
  const { h, dom, fmt } = ctx;
  const icon = (d) => dom.svgIcon(d);

  const play = h('button', { class: 'icon-btn timeline-play', type: 'button', 'aria-label': 'Play', title: 'Play (Space)' }, icon(ICON_PLAY));
  const speedBtns = SPEEDS.map(s => h('button', { type: 'button', role: 'radio', 'aria-checked': 'false', title: s.title,
    dataset: { v: s.v } }, s.label));
  const speeds = h('div', { class: 'seg timeline-speeds', role: 'radiogroup', 'aria-label': 'Playback speed' }, speedBtns);
  const back = h('button', { class: 'icon-btn timeline-step', type: 'button', title: 'One hour back', 'aria-label': 'One hour back' }, '−1 h');
  const fwd = h('button', { class: 'icon-btn timeline-step', type: 'button', title: 'One hour forward', 'aria-label': 'One hour forward' }, '+1 h');

  const svgEl = svg('svg', { class: 'timeline-svg', height: TRACK_H, 'aria-hidden': 'true' });
  const gGrid = svg('g', { class: 'timeline-grid' });
  const gHist = svg('g', { class: 'timeline-hist' });
  const gStrips = svg('g', { class: 'timeline-strips' });
  svgEl.append(gGrid, gHist, gStrips);
  const head = h('div', { class: 'timeline-head', 'aria-hidden': 'true' }, h('span', { class: 'timeline-knob' }));
  const hover = h('div', { class: 'timeline-hoverline', hidden: true, 'aria-hidden': 'true' });
  const empty = h('div', { class: 'timeline-empty', hidden: true });
  const track = h('div', { class: 'timeline-track', role: 'slider', tabindex: '0', 'aria-label': 'Replay time',
    'aria-valuemin': '0', 'aria-valuemax': '0', 'aria-valuenow': '0' }, svgEl, hover, head, empty);
  const tickRow = h('div', { class: 'timeline-ticks', 'aria-hidden': 'true' });
  const labels = h('div', { class: 'timeline-rowlabels', 'aria-hidden': 'true' },
    h('span', { class: 'timeline-rl-events' }, 'Events'), h('span', null, 'Aircraft'), h('span', null, 'Ships'));
  const tip = h('div', { class: 'timeline-tip', hidden: true, role: 'tooltip' });

  const clock = h('time', { class: 'timeline-clock' }, '');
  const day = h('span', { class: 'timeline-day' }, '');
  const note = h('span', { class: 'timeline-note' }, '');
  const liveBtn = h('button', { class: 'btn timeline-live', type: 'button', title: 'Back to live (End)' }, 'Back to live');
  const close = h('button', { class: 'icon-btn timeline-close', type: 'button', 'aria-label': 'Close the timeline', title: 'Close the timeline' }, icon(ICON_CLOSE));

  const el = h('section', { class: 'timeline-bar', 'aria-label': 'Timeline' },
    h('div', { class: 'timeline-left' }, play, speeds, h('div', { class: 'timeline-steps' }, back, fwd)),
    h('div', { class: 'timeline-main' }, labels, h('div', { class: 'timeline-trackwrap' }, track, tickRow)),
    h('div', { class: 'timeline-right' }, h('div', { class: 'timeline-readout' }, clock, day, note), liveBtn, close),
    tip);

  const st = { start: 0, end: 1, t: null, mode: 'live', hours: [], air: [], sea: [], width: 0, dragging: false,
    playing: false, speed: 300, hoverT: null };

  // ---------------------------------------------------------------- geometry
  const xOf = (t) => (st.end > st.start ? (t - st.start) / (st.end - st.start) * st.width : 0);
  const tOf = (x) => st.start + Math.max(0, Math.min(1, x / Math.max(1, st.width))) * (st.end - st.start);

  function measure() {
    st.width = Math.max(1, track.clientWidth);
  }

  function draw() {
    measure();
    const w = st.width;
    svgEl.setAttribute('width', w);
    svgEl.setAttribute('viewBox', `0 0 ${w} ${TRACK_H}`);
    dom.clear(gGrid);
    dom.clear(gHist);
    dom.clear(gStrips);
    dom.clear(tickRow);
    // hairline baseline and ticks
    gGrid.append(svg('line', { x1: 0, x2: w, y1: HIST_H + 0.5, y2: HIST_H + 0.5, class: 'timeline-base' }));
    for (const tk of ticks(st.start, st.end, w)) {
      gGrid.append(svg('line', { x1: Math.round(tk.x) + 0.5, x2: Math.round(tk.x) + 0.5, y1: 0, y2: TRACK_H, class: tk.day ? 'timeline-gridday' : 'timeline-gridline' }));
      if (tk.x > 18 && tk.x < w - 18) tickRow.append(h('span', { class: tk.day ? 'day' : '', style: { left: `${tk.x}px` } }, tk.label));
    }
    // histogram: one column per UTC hour, square at the baseline, rounded at the top
    const span = Math.max(1, st.end - st.start);
    const slot = w * 3600 / span;
    const colW = Math.max(1, Math.min(24, slot - 2));
    const max = Math.max(1, ...st.hours.map(b => b.count));
    for (const b of st.hours) {
      if (!b.count || b.t + 3600 <= st.start || b.t >= st.end) continue;
      const x0 = Math.max(0, xOf(b.t));
      const x1 = Math.min(w, xOf(b.t + 3600));
      const cw = Math.min(colW, Math.max(1, x1 - x0 - 2));
      const x = x0 + (x1 - x0 - cw) / 2;
      // Log scale: ongoing events all count in the hour of their latest report, so the current hour
      // dwarfs the rest on a linear one. The tooltip gives the exact count.
      const hgt = Math.max(2, Math.round(Math.log1p(b.count) / Math.log1p(max) * (HIST_H - 3)));
      const level = severityLevel(b.max_severity);
      gHist.append(svg('path', { d: column(x, cw, hgt, HIST_H, cw >= 8 ? 3 : 1.5), fill: SEVERITY[level].color, class: `lv-${level}` }));
      if (level === 'extreme') gHist.append(svg('rect', { x, y: HIST_H - hgt - 3, width: cw, height: 2, rx: 1, class: 'timeline-cap' }));
    }
    // coverage: contiguous hours with recorded positions
    strips(st.air, STRIP_Y[0], GROUP_AIR);
    strips(st.sea, STRIP_Y[1], GROUP_SEA);
    place();
  }

  function strips(hours, y, color) {
    const runs = [];
    for (const b of [...hours].sort((a, c) => a.t - c.t)) {
      if (!b.samples) continue;
      const last = runs[runs.length - 1];
      if (last && last[1] === b.t) last[1] = b.t + 3600; else runs.push([b.t, b.t + 3600]);
    }
    for (const [a, b] of runs) {
      const x0 = Math.max(0, xOf(Math.max(a, st.start)));
      const x1 = Math.min(st.width, xOf(Math.min(b, st.end)));
      if (x1 - x0 < 0.5) continue;
      gStrips.append(svg('rect', { x: x0, y: y - 2, width: Math.max(2, x1 - x0), height: 4, rx: 2, fill: color }));
    }
  }

  function place() {
    const t = st.t == null ? st.end : st.t;
    const x = Math.round(xOf(Math.min(st.end, Math.max(st.start, t))));
    head.style.transform = `translateX(${x}px)`;
    head.classList.toggle('live', st.mode !== 'replay');
    // Screen readers hear the slider by the minute, not every animation frame of playback.
    const text = st.mode === 'replay' ? `Replay at ${whenText(t)}` : `Live, ${whenText(st.end)}`;
    if (text === st.aria) return;
    st.aria = text;
    track.setAttribute('aria-valuemin', String(Math.round(st.start)));
    track.setAttribute('aria-valuemax', String(Math.round(st.end)));
    track.setAttribute('aria-valuenow', String(Math.round(t / 60) * 60));
    track.setAttribute('aria-valuetext', text);
  }

  function readout() {
    if (st.mode === 'replay' && st.t != null) {
      clock.textContent = clockText(st.t, true);
      clock.setAttribute('datetime', new Date(st.t * 1000).toISOString());
      day.textContent = `${dateText(st.t)}, UTC`;
    } else {
      clock.textContent = 'Live';
      clock.removeAttribute('datetime');
      day.textContent = `${clockText(Date.now() / 1000)} UTC, ${dateText(Date.now() / 1000)}`;
    }
    el.classList.toggle('replaying', st.mode === 'replay');
    liveBtn.disabled = st.mode !== 'replay';
    liveBtn.classList.toggle('primary', st.mode === 'replay');
  }

  // ---------------------------------------------------------------- hover tooltip
  function showTip(x) {
    const t = tOf(x);
    const hour = Math.floor(t / 3600) * 3600;
    const bin = st.hours.find(b => b.t === hour);
    const air = st.air.find(b => b.t === hour);
    const sea = st.sea.find(b => b.t === hour);
    dom.clear(tip);
    tip.append(h('strong', null, `${clockText(hour)} to ${clockText(hour + 3600)} UTC, ${dateText(hour)}`));
    if (bin && bin.count) {
      const level = severityLevel(bin.max_severity);
      tip.append(h('span', { class: 'timeline-tiprow' }, h('i', { class: 'key', style: { background: SEVERITY[level].color } }),
        `${fmt.int(bin.count)} event${bin.count === 1 ? '' : 's'} in view, highest severity ${fmt.num(bin.max_severity, bin.max_severity % 1 ? 1 : 0)} (${SEVERITY[level].label})`));
    } else {
      tip.append(h('span', { class: 'timeline-tiprow dim' }, 'No events in view'));
    }
    if (air && air.samples) tip.append(h('span', { class: 'timeline-tiprow' }, h('i', { class: 'key line', style: { background: GROUP_AIR } }), `Aircraft: ${fmt.int(air.samples)} positions recorded`));
    if (sea && sea.samples) tip.append(h('span', { class: 'timeline-tiprow' }, h('i', { class: 'key line', style: { background: GROUP_SEA } }), `Ships: ${fmt.int(sea.samples)} positions recorded`));
    if (!(air && air.samples) && !(sea && sea.samples)) tip.append(h('span', { class: 'timeline-tiprow dim' }, 'No aircraft or ship positions recorded'));
    tip.hidden = false;
    hover.hidden = false;
    hover.style.transform = `translateX(${Math.round(x)}px)`;
    const box = el.getBoundingClientRect();
    const tr = track.getBoundingClientRect();
    const left = tr.left - box.left + x;
    const tw = tip.offsetWidth;
    tip.style.left = `${Math.max(6, Math.min(box.width - tw - 6, left - tw / 2))}px`;
  }

  function hideTip() {
    tip.hidden = true;
    hover.hidden = true;
  }

  // ---------------------------------------------------------------- pointer and keys
  const xFromEvent = (e) => e.clientX - track.getBoundingClientRect().left;
  track.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    track.focus({ preventScroll: true });
    track.setPointerCapture(e.pointerId);
    st.dragging = true;
    el.classList.add('dragging');
    hideTip();
    on.seek(tOf(xFromEvent(e)), { final: false });
  });
  track.addEventListener('pointermove', (e) => {
    if (st.dragging) on.seek(tOf(xFromEvent(e)), { final: false });
    else showTip(xFromEvent(e));
  });
  const endDrag = (e) => {
    if (!st.dragging) return;
    st.dragging = false;
    el.classList.remove('dragging');
    try { track.releasePointerCapture(e.pointerId); } catch { /* already released */ }
    on.seek(tOf(xFromEvent(e)), { final: true });
  };
  track.addEventListener('pointerup', endDrag);
  track.addEventListener('pointercancel', endDrag);
  track.addEventListener('pointerleave', () => { if (!st.dragging) hideTip(); });
  track.addEventListener('keydown', (e) => {
    const now = st.t == null ? st.end : st.t;
    const step = { ArrowLeft: -300, ArrowRight: 300, PageDown: -3600, PageUp: 3600 }[e.key];
    if (step != null) {
      e.preventDefault();
      on.seek(now + (e.shiftKey && Math.abs(step) === 300 ? step / 5 : step), { final: true });
    } else if (e.key === 'Home') { e.preventDefault(); on.seek(st.start, { final: true }); }
    else if (e.key === 'End') { e.preventDefault(); on.live(); }
  });
  el.addEventListener('keydown', (e) => {
    if (e.key === ' ' && !e.target.closest('button')) { e.preventDefault(); e.stopPropagation(); on.toggle(); }
    else if (e.key === 'Escape') { e.preventDefault(); on.close(); }
  });

  play.addEventListener('click', () => on.toggle());
  for (const b of speedBtns) b.addEventListener('click', () => on.speed(+b.dataset.v));
  back.addEventListener('click', () => on.step(-3600));
  fwd.addEventListener('click', () => on.step(3600));
  liveBtn.addEventListener('click', () => on.live());
  close.addEventListener('click', () => on.close());

  const ro = new ResizeObserver(() => { if (el.isConnected) draw(); });
  ro.observe(track);

  // ---------------------------------------------------------------- public
  return {
    el,
    setRange(start, end) { st.start = start; st.end = Math.max(start + 60, end); draw(); },
    setData({ hours, air, sea }) { st.hours = hours || []; st.air = air || []; st.sea = sea || []; draw(); },
    setTime(t, mode) { st.t = t; st.mode = mode; place(); readout(); },
    setPlaying(v) {
      st.playing = !!v;
      dom.clear(play);
      play.append(icon(v ? ICON_PAUSE : ICON_PLAY));
      play.setAttribute('aria-label', v ? 'Pause' : 'Play');
      play.title = v ? 'Pause (Space)' : 'Play (Space)';
      el.classList.toggle('playing', !!v);
    },
    setSpeed(v) {
      st.speed = v;
      for (const b of speedBtns) b.setAttribute('aria-checked', String(+b.dataset.v === v));
    },
    setNote(text, tone = '') { note.textContent = text || ''; note.className = `timeline-note${tone ? ` ${tone}` : ''}`; },
    setEmpty(text) { empty.hidden = !text; empty.textContent = text || ''; },
    focus() { track.focus({ preventScroll: true }); },
    dragging: () => st.dragging,
    destroy() { ro.disconnect(); },
  };
}
