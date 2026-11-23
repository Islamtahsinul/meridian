// Pure helpers for the Alerts front end: quiet hours, who gets notified, tones and a preview circle.
// No DOM here, so Node can test it (tests/fixtures/alerts/logic_test.mjs).

export const POLL_MS = 45000;
export const NOTIFY_AT_ONCE = 3;          // more than this in one poll: one summary for the rest
export const EXTREME = 9;

/** 'red' (9 and more, extreme), 'amber' (6 to 8, serious) or 'blue' (notable and routine). */
export function toneOf(severity) {
  const s = Number(severity) || 0;
  return s >= EXTREME ? 'red' : s >= 6 ? 'amber' : 'blue';
}

export function toneColor(tone) {
  return { red: '#ff5d52', amber: '#ffb547', green: '#5fd38d', blue: '#6cb6ff' }[tone] || '#6cb6ff';
}

function minutes(text) {
  const m = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(String(text || '').trim());
  return m ? (+m[1]) * 60 + (+m[2]) : null;
}

/** Quiet hours use this computer's clock. A window like 22:00 to 07:00 runs past midnight. */
export function inQuietHours(settings, date = new Date()) {
  const q = settings?.quiet_hours;
  if (!q?.on) return false;
  const start = minutes(q.start);
  const end = minutes(q.end);
  if (start == null || end == null || start === end) return false;
  const now = date.getHours() * 60 + date.getMinutes();
  return start < end ? now >= start && now < end : now >= start || now < end;
}

/** Should this new alert pop up (toast and, when allowed, a desktop notification)? */
export function shouldNotify(alert, settings, date = new Date()) {
  if (!alert || alert.folded) return false;
  if (alert.reason === 'test') return true;                 // the user asked for it
  const n = settings?.notify || { on: true, min_severity: 0 };
  if (n.on === false) return false;
  const sev = Number(alert.severity) || 0;
  if (sev < (Number(n.min_severity) || 0)) return false;
  if (inQuietHours(settings, date)) return !!settings.quiet_hours.allow_extreme && sev >= EXTREME;
  return true;
}

/** Which of the new alerts to announce one by one, and how many more to sum up in one line. */
export function plan(alerts, settings, date = new Date()) {
  const eligible = (alerts || []).filter(a => shouldNotify(a, settings, date))
    .sort((a, b) => (b.severity || 0) - (a.severity || 0) || b.id - a.id);
  return { each: eligible.slice(0, NOTIFY_AT_ONCE), more: Math.max(0, eligible.length - NOTIFY_AT_ONCE),
    top: eligible[0] || null };
}

// ------------------------------------------------------------------ geometry for previews
const R = 6371.0088;
const RAD = Math.PI / 180;

function unwrapNear(lon, ref) {
  let x = lon;
  while (x - ref > 180) x -= 360;
  while (x - ref < -180) x += 360;
  return x;
}

/** A geodesic circle as a GeoJSON Polygon (for the "place and radius" preview; the server keeps its own). */
export function circlePolygon(lat, lon, radiusKm, steps = 64) {
  const la = Math.max(-89.9, Math.min(89.9, +lat));
  const toPole = (90 - Math.abs(la)) * RAD * R - 1;
  const r = Math.max(0.01, Math.min(+radiusKm || 0, 5000, toPole));
  const d = r / R;
  const p1 = la * RAD;
  const l1 = +lon * RAD;
  const ring = [];
  for (let i = 0; i < steps; i++) {
    const b = (-i * 360 / steps) * RAD;
    const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
    const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
    const x = ((l2 / RAD + 540) % 360) - 180;
    ring.push([Math.round(unwrapNear(x, +lon) * 1e5) / 1e5, Math.round((p2 / RAD) * 1e5) / 1e5]);
  }
  ring.push([...ring[0]]);
  return { type: 'Polygon', coordinates: [ring] };
}

/** Short plain-language age: "just now", "4 min ago", "3 h ago", "2 days ago". */
export function age(tSeconds, nowMs = Date.now()) {
  const s = Math.max(0, Math.round(nowMs / 1000 - (+tSeconds || 0)));
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

/** True for a plain Escape press (no modifier, not ending an IME composition). */
export function isPlainEscape(e) {
  return !!e && e.key === 'Escape' && !e.isComposing && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey;
}

/** Keyboard movement in a tab list (WAI-ARIA tabs pattern): the index to move to, or null for other keys. */
export function tabTarget(key, index, count) {
  if (!(count > 0)) return null;
  const i = Number.isInteger(index) && index >= 0 && index < count ? index : 0;
  if (key === 'ArrowRight') return (i + 1) % count;
  if (key === 'ArrowLeft') return (i - 1 + count) % count;
  if (key === 'Home') return 0;
  if (key === 'End') return count - 1;
  return null;
}
