// Timeline replay maths (no DOM, no map): where an aircraft or vessel was at time t, short trails,
// how visible an event is at t, severity levels and time-axis ticks. Times are epoch seconds.
// Tested by tests/test_timeline_web.mjs.
//
// A track is {s: [[t, lat, lon, a, b, flags], ...]} sorted by t, where a/b are altitude (ft) and
// track (deg) for aircraft, speed (kn) and course (deg) for vessels.

const RAD = Math.PI / 180;

// Aircraft: samples are 60 s to 5 min apart while received (flights/history.py). Interpolate across
// gaps up to 15 min; after the last sample hold it 2 min, as the live map hides an aircraft about
// a minute or two after its last report.
export const AIR = { gap: () => 15 * 60, hold: () => 120, stale: 75, trail: 4 * 60 };

// Vessels: samples 2 to 10 min apart under way, 2 h when stationary (ships/store.py). Interpolate
// across an hour (7.5 h between two stationary samples); after the last sample hold it as the live
// map does: 1 h under way, 6 h stationary, drawn as stale after 15 min and 1 h.
export const SEA = {
  gap: (a, b) => (still(a) && still(b) ? 7.5 * 3600 : 3600),
  hold: (a) => (still(a) ? 6 * 3600 : 3600),
  stale: (a) => (still(a) ? 3600 : 900),
  trail: 60 * 60,
};

export function still(sample) {
  return !(sample && sample[3] != null && sample[3] >= 1);
}

/** Largest index i with s[i][0] <= t, or -1. */
export function lowerIndex(s, t) {
  let lo = 0;
  let hi = s.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (s[mid][0] <= t) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found;
}

export function wrapLon(lon) {
  if (lon >= -180 && lon <= 180) return lon;
  const x = ((lon + 180) % 360 + 360) % 360 - 180;
  return x === -180 && lon > 0 ? 180 : x;
}

/** Shortest signed difference b - a in degrees (-180..180]. */
export function angleDiff(a, b) {
  const d = ((b - a) % 360 + 540) % 360 - 180;
  return d === -180 ? 180 : d;
}

export function lerpAngle(a, b, f) {
  if (a == null) return b == null ? null : b;
  if (b == null) return a;
  return ((a + angleDiff(a, b) * f) % 360 + 360) % 360;
}

export function bearing(lat1, lon1, lat2, lon2) {
  const p1 = lat1 * RAD;
  const p2 = lat2 * RAD;
  const dl = (lon2 - lon1) * RAD;
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(y, x) / RAD + 360) % 360;
}

export function haversineKm(lat1, lon1, lat2, lon2) {
  const dp = (lat2 - lat1) * RAD;
  const dl = (lon2 - lon1) * RAD;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(lat1 * RAD) * Math.cos(lat2 * RAD) * Math.sin(dl / 2) ** 2;
  return 2 * 6371.0088 * Math.asin(Math.min(1, Math.sqrt(a)));
}

// Below this separation (degrees, longitude scaled by latitude) a straight line in latitude and
// longitude is within ~200 m of the great circle and far cheaper to compute every frame.
const LINEAR_DEG = 1.2;

/** Point a fraction f along the path from (lat1, lon1) to (lat2, lon2): [lat, lon]. A straight
 *  line between close samples, the great circle between distant ones. */
export function between(lat1, lon1, lat2, lon2, f) {
  const dl = angleDiff(lon1, lon2);
  if (Math.abs(lat2 - lat1) < LINEAR_DEG && Math.abs(dl) * Math.cos(lat1 * RAD) < LINEAR_DEG) {
    return [lat1 + (lat2 - lat1) * f, wrapLon(lon1 + dl * f)];
  }
  return greatCircle(lat1, lon1, lat2, lon2, f);
}

/** Point a fraction f along the great circle from (lat1, lon1) to (lat2, lon2): [lat, lon]. */
export function greatCircle(lat1, lon1, lat2, lon2, f) {
  const p1 = lat1 * RAD;
  const p2 = lat2 * RAD;
  const l1 = lon1 * RAD;
  const l2 = lon2 * RAD;
  const x1 = Math.cos(p1) * Math.cos(l1); const y1 = Math.cos(p1) * Math.sin(l1); const z1 = Math.sin(p1);
  const x2 = Math.cos(p2) * Math.cos(l2); const y2 = Math.cos(p2) * Math.sin(l2); const z2 = Math.sin(p2);
  const dot = Math.max(-1, Math.min(1, x1 * x2 + y1 * y2 + z1 * z2));
  const omega = Math.acos(dot);
  if (omega < 1e-9) return [lat1, lon1];
  if (omega < 1e-4) {                       // under ~600 m: a straight line is exact enough
    return [lat1 + (lat2 - lat1) * f, wrapLon(lon1 + angleDiff(lon1, lon2) * f)];
  }
  const s = Math.sin(omega);
  const a = Math.sin((1 - f) * omega) / s;
  const b = Math.sin(f * omega) / s;
  const x = a * x1 + b * x2; const y = a * y1 + b * y2; const z = a * z1 + b * z2;
  return [Math.atan2(z, Math.hypot(x, y)) / RAD, wrapLon(Math.atan2(y, x) / RAD)];
}

/**
 * Where a track was at time t under rules (AIR or SEA), or null when it should not be drawn:
 * {lat, lon, a, b, dir, flags, i, f, state: 'exact'|'between'|'held', age, stale, t0, t1}.
 * `dir` is the heading for the icon: the reported track/course, else the bearing between samples.
 * With detail (cards), also speedKn estimated from the samples around t.
 */
export function positionAt(track, t, rules, detail = false) {
  const s = track && track.s;
  if (!s || !s.length) return null;
  const i = lowerIndex(s, t);
  if (i < 0) return null;
  const p = s[i];
  const q = s[i + 1];
  if (p[0] === t) return held(p, i, 0, 'exact', rules, s, detail);
  if (q && q[0] - p[0] <= rules.gap(p, q)) {
    const f = (t - p[0]) / (q[0] - p[0]);
    const [lat, lon] = between(p[1], p[2], q[1], q[2], f);
    const a = p[3] != null && q[3] != null ? p[3] + (q[3] - p[3]) * f : (f < 0.5 ? p[3] : q[3]);
    let b = lerpAngle(p[4], q[4], f);
    if (b == null && moved(p, q)) b = bearing(p[1], p[2], q[1], q[2]);
    return { lat, lon, a, b, dir: b, flags: f < 0.5 ? p[5] : q[5], i, f, state: 'between', age: 0, stale: false,
      t0: p[0], t1: q[0], speedKn: detail ? speed(p, q) : null };
  }
  const age = t - p[0];
  if (age > rules.hold(p)) return null;
  return held(p, i, age, age > 0 ? 'held' : 'exact', rules, s, detail);
}

/** Did it move more than ~50 m between two samples? (A heading needs real movement.) */
function moved(p, q) {
  return Math.abs(q[1] - p[1]) + Math.abs(q[2] - p[2]) * Math.cos(p[1] * RAD) > 4.5e-4;
}

function speed(p, q) {
  return q[0] > p[0] ? haversineKm(p[1], p[2], q[1], q[2]) / ((q[0] - p[0]) / 3600) / 1.852 : null;
}

function held(p, i, age, state, rules, s, detail) {
  const stale = typeof rules.stale === 'function' ? age > rules.stale(p) : age > rules.stale;
  // Heading (and with detail, speed) from the previous sample when it was close enough to interpolate.
  const o = s && i > 0 ? s[i - 1] : null;
  const near = o && p[0] > o[0] && p[0] - o[0] <= rules.gap(o, p);
  let dir = p[4];
  if (dir == null && near && moved(o, p)) dir = bearing(o[1], o[2], p[1], p[2]);
  return { lat: p[1], lon: wrapLon(p[2]), a: p[3], b: p[4], dir, flags: p[5], i, f: 0, state, age, stale,
    t0: p[0], t1: p[0], speedKn: detail && near ? speed(o, p) : null };
}

/** Trail coordinates [[lon, lat], ...] from samples in (t - span, t] up to the current position,
 *  with longitudes unwrapped so a trail across the antimeridian stays continuous. */
export function trail(track, t, span, pos) {
  const s = track && track.s;
  if (!s || !pos) return null;
  const out = [];
  let i = pos.i;
  const from = t - span;
  const start = [];
  while (i >= 0 && s[i][0] > from) { start.push(s[i]); i--; }
  start.reverse();
  for (const p of start) out.push([p[2], p[1]]);
  if (!out.length || out[out.length - 1][0] !== pos.lon || out[out.length - 1][1] !== pos.lat) out.push([pos.lon, pos.lat]);
  if (out.length < 2) return null;
  for (let k = 1; k < out.length; k++) out[k][0] = out[k - 1][0] + angleDiff(out[k - 1][0], out[k][0]);
  return out;
}

// ------------------------------------------------------------------ events
export const EVENT_FADE_IN = 300;      // replay seconds from first sight to full strength

/** How long an event stays on the replay map after its span ends, by severity. */
export function linger(severity) {
  const s = +severity || 0;
  if (s >= 9) return 12 * 3600;
  if (s >= 6) return 6 * 3600;
  if (s >= 3) return 3 * 3600;
  return 3600;
}

/** Visibility 0..1 of an event {t0: first_time, t1: time, sev} at replay time t: it appears at its
 *  first time, reaches full strength over EVENT_FADE_IN, stays while active and then fades out
 *  over linger(severity). Never before it happened. */
export function eventAlpha(ev, t) {
  const t0 = ev.t0 ?? ev.t1;
  const end = Math.max(ev.t1 ?? t0, t0 + EVENT_FADE_IN);
  if (t < t0) return 0;
  const fadeIn = Math.min(1, 0.3 + 0.7 * (t - t0) / EVENT_FADE_IN);
  if (t <= end) return fadeIn;
  const out = 1 - (t - end) / linger(ev.sev);
  return out <= 0 ? 0 : out;
}

export function severityLevel(sev) {
  const s = +sev || 0;
  if (s >= 9) return 'extreme';
  if (s >= 6) return 'serious';
  if (s >= 3) return 'notable';
  return 'routine';
}

// ------------------------------------------------------------------ the time axis
const TICK_STEPS = [1, 2, 3, 6, 12, 24].map(h => h * 3600);

/** Hour ticks for [start, end] (epoch s) on a track `width` px wide: [{t, x, label, day}]. */
export function ticks(start, end, width, minGap = 56) {
  if (!(end > start) || !(width > 0)) return [];
  const pxPerS = width / (end - start);
  const step = TICK_STEPS.find(st => st * pxPerS >= minGap) || TICK_STEPS[TICK_STEPS.length - 1];
  const out = [];
  for (let t = Math.ceil(start / step) * step; t <= end; t += step) {
    const d = new Date(t * 1000);
    const day = d.getUTCHours() === 0;
    out.push({ t, x: (t - start) * pxPerS, day,
      label: day ? `${DAYS[d.getUTCDay()]} ${d.getUTCDate()}` : `${String(d.getUTCHours()).padStart(2, '0')}:00` });
  }
  return out;
}

export const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
export const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad = n => String(n).padStart(2, '0');

/** "14:05" or "14:05:30" (UTC). */
export function clockText(t, seconds = false) {
  const d = new Date(t * 1000);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${seconds ? `:${pad(d.getUTCSeconds())}` : ''}`;
}

/** "Mon 23 Nov" (UTC). */
export function dateText(t) {
  const d = new Date(t * 1000);
  return `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

/** "14:05 UTC" today, "Mon 23 Nov, 14:05 UTC" otherwise (relative to `now`, epoch s). */
export function whenText(t, now = Date.now() / 1000, seconds = false) {
  const same = new Date(t * 1000).toISOString().slice(0, 10) === new Date(now * 1000).toISOString().slice(0, 10);
  return same ? `${clockText(t, seconds)} UTC` : `${dateText(t)}, ${clockText(t, seconds)} UTC`;
}

/** 95 -> "1 min 35 s", 4000 -> "1 h 7 min". */
export function spanText(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return s % 60 && s < 600 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${Math.round(s / 60)} min`;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

/** Chunk start times (multiples of size) covering [from, to]. */
export function chunksFor(from, to, size) {
  const out = [];
  for (let c = Math.floor(from / size) * size; c <= to; c += size) out.push(c);
  return out;
}

/** Does box a [w, s, e, n] contain box b? (No antimeridian wrap: both come from viewBounds.) */
export function boxContains(a, b) {
  if (!a || !b) return false;
  if (!(a[1] <= b[1] && a[3] >= b[3])) return false;
  if (a[2] - a[0] >= 360 || (a[0] <= -180 && a[2] >= 180)) return true;
  const aw = a[0]; const ae = a[2] < a[0] ? a[2] + 360 : a[2];
  let bw = b[0]; let be = b[2] < b[0] ? b[2] + 360 : b[2];
  if (bw < aw) { bw += 360; be += 360; }
  return aw <= bw && ae >= be;
}

/** A view box padded by `frac` on every side, clamped to the world. */
export function padBox(b, frac = 0.25) {
  const [w, s, e, n] = b;
  const width = e >= w ? e - w : e + 360 - w;
  if (width >= 300) return [-180, Math.max(-85, s - (n - s) * frac), 180, Math.min(85, n + (n - s) * frac)];
  const dx = width * frac;
  const dy = (n - s) * frac;
  if (width + 2 * dx >= 360) return [-180, Math.max(-85, s - dy), 180, Math.min(85, n + dy)];
  return [+wrapLon(w - dx).toFixed(3), +Math.max(-85, s - dy).toFixed(3), +wrapLon(e + dx).toFixed(3), +Math.min(85, n + dy).toFixed(3)];
}
