// Replay clock (CONTRACT.md section 15.3). Meridian is live unless something (the Timeline's time
// bar) puts it into replay. ctx.time.now() is the time the map shows, in epoch ms.
//
//   ctx.time.mode()        'live' or 'replay'
//   ctx.time.now()         Date.now() when live, the replay time when replaying (ms)
//   ctx.time.setReplay(t)  enter or move replay (ms; seconds, ISO strings and Dates are accepted;
//                          the future is clamped to now). Returns the replay time in ms.
//   ctx.time.live()        back to live
//
// Bus events: 'time:mode' {mode} when the mode changes, then 'time:changed' {t, mode} whenever the
// shown time jumps (each setReplay that moves it, and once on returning to live). Live time does not
// tick on the bus: read ctx.time.now() or Date.now(). Replay is not saved: a reload is live.

import { bus } from './bus.js';
import { toMs } from './dom.js';

let current = 'live';
let replayMs = null;

export function mode() {
  return current;
}

export function now() {
  return current === 'replay' ? replayMs : Date.now();
}

export function setReplay(t) {
  const raw = toMs(t);
  if (!Number.isFinite(raw) || raw < 0) throw new TypeError('time.setReplay needs a time in epoch milliseconds');
  const ms = Math.min(Math.round(raw), Date.now());
  const entering = current !== 'replay';
  if (!entering && ms === replayMs) return ms;
  current = 'replay';
  replayMs = ms;
  if (entering) bus.emit('time:mode', { mode: 'replay' });
  bus.emit('time:changed', { t: ms, mode: 'replay' });
  return ms;
}

export function live() {
  if (current === 'live') return;
  current = 'live';
  replayMs = null;
  bus.emit('time:mode', { mode: 'live' });
  bus.emit('time:changed', { t: Date.now(), mode: 'live' });
}

export const time = { mode, now, setReplay, live };
