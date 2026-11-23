// Shared constants and helpers for the ships module (browser side).
// The class order matches meridian/modules/ships/classify.py (the API sends class indexes).
//
// Colour: two validated triads on the dark water surface (#2c353c), separated by icon shape.
// Large-ship arrows (hull): cargo, tanker, passenger. Small-craft darts: fishing, service, pleasure.
// High-speed, military and other ride on shape, legend, tooltip and the type filter.

export const GROUP_COLOR = '#4fa8e8';
export const INK = '#111a2c';
export const PAPER = '#ebe6d6';
export const RED = '#ff5d52';
export const AMBER = '#ffb547';

export const CLASSES = [
  { id: 'cargo', label: 'Cargo', color: '#3987e5', shape: 'hull' },
  { id: 'tanker', label: 'Tanker', color: '#d95926', shape: 'hull' },
  { id: 'passenger', label: 'Passenger', color: '#199e70', shape: 'hull' },
  { id: 'fishing', label: 'Fishing', color: '#c98500', shape: 'dart' },
  { id: 'service', label: 'Tug, pilot, SAR or law enforcement', short: 'Service craft', color: '#9085e9', shape: 'dart' },
  { id: 'pleasure', label: 'Sailing or pleasure', color: '#d55181', shape: 'dart' },
  { id: 'highspeed', label: 'High-speed craft', color: '#0ca30c', shape: 'dart' },
  { id: 'military', label: 'Military', color: '#ebe6d6', shape: 'hull' },
  { id: 'other', label: 'Other or unknown', color: '#8b97b0', shape: 'dart' },
];
export const CLASS_BY_ID = Object.fromEntries(CLASSES.map((c, i) => [c.id, { ...c, index: i }]));

export const F_SANCTIONED = 1;
export const F_POSSIBLE = 2;
export const F_CLASS_B = 4;
export const F_STALE = 8;
export const F_SUSPECT = 16;

export function classAt(index) {
  return CLASSES[index] || CLASSES[CLASSES.length - 1];
}

export function classOf(id) {
  return CLASS_BY_ID[id] || CLASS_BY_ID.other;
}

/** Icon scale per size class (0 unknown, 1 under 40 m, 2 from 40 m, 3 from 150 m, 4 from 250 m). */
export const SIZE_FACTOR = [0.9, 0.78, 0.92, 1.1, 1.28];

// ------------------------------------------------------------------ icons
// Pre-coloured icons (one per class) with an ink outline stay crisp at every zoom; SDF tinting of
// a hard-edged shape blurs the outline and leaves no room for a halo.
function hullPath(k) {
  const p = new Path2D();
  p.moveTo(16 * k, 3 * k);          // bow
  p.lineTo(22 * k, 11.5 * k);
  p.lineTo(22 * k, 28 * k);
  p.lineTo(10 * k, 28 * k);         // square stern
  p.lineTo(10 * k, 11.5 * k);
  p.closePath();
  return p;
}

function dartPath(k) {
  const p = new Path2D();
  p.moveTo(16 * k, 4 * k);
  p.lineTo(23.5 * k, 27 * k);
  p.lineTo(16 * k, 21.5 * k);
  p.lineTo(8.5 * k, 27 * k);
  p.closePath();
  return p;
}

export function iconName(cls) {
  return `ships-${cls}`;
}

export function registerIcons(map2) {
  for (const c of CLASSES) {
    map2.icon(iconName(c.id), (g, s) => {
      const k = s / 32;
      const p = c.shape === 'hull' ? hullPath(k) : dartPath(k);
      g.lineJoin = 'round';
      g.lineWidth = 2.4 * k;
      g.strokeStyle = INK;
      g.stroke(p);
      g.fillStyle = c.color;
      g.fill(p);
      g.lineWidth = 0.9 * k;
      g.strokeStyle = 'rgba(255,255,255,0.35)';
      g.stroke(p);
    }, { size: 32, sdf: false });
  }
}

// ------------------------------------------------------------------ formatting
export function knots(fmt, sog) {
  return sog == null ? null : fmt.num(sog, 1);
}

export function degrees(v) {
  return v == null ? null : String(Math.round(v) % 360).padStart(3, '0');
}

export function hours(seconds) {
  if (seconds == null) return '–';
  const h = seconds / 3600;
  return h < 10 ? `${h.toFixed(1)} h` : `${Math.round(h)} h`;
}

export function stateLabel(state) {
  return ({ underway: 'Under way', anchored: 'At anchor', moored: 'Moored or stopped' })[state] || null;
}

/** Year-less AIS ETA ("11-25T06:30Z") resolved by the server to eta_t. */
export function etaText(fmt, eta, etaT, past) {
  if (!eta) return null;
  if (etaT) {
    const d = new Date(etaT * 1000);
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const day = `${d.getUTCDate()} ${months[d.getUTCMonth()]}`;
    const time = eta.length > 5 ? ` ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC` : '';
    return `${day}${time}${past ? ' (in the past)' : ''}`;
  }
  return eta;
}

/** Geodesic circle as a GeoJSON polygon ring (lon/lat), for uncertainty circles. */
export function circlePolygon(lat, lon, radiusKm, steps = 64) {
  const R = 6371.0088;
  const d = radiusKm / R;
  const p1 = lat * Math.PI / 180;
  const l1 = lon * Math.PI / 180;
  const ring = [];
  for (let i = 0; i <= steps; i++) {
    const b = (i / steps) * 2 * Math.PI;
    const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
    const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
    ring.push([+(((l2 * 180 / Math.PI) + 540) % 360 - 180).toFixed(5), +(p2 * 180 / Math.PI).toFixed(5)]);
  }
  return { type: 'Polygon', coordinates: [ring] };
}

/** Ray-casting point in polygon for GeoJSON Polygon / MultiPolygon geometry. */
export function pointInGeometry(lon, lat, geom) {
  const inRing = (ring) => {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];
      if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / ((yj - yi) || 1e-12) + xi) inside = !inside;
    }
    return inside;
  };
  const inPoly = (poly) => inRing(poly[0]) && !poly.slice(1).some(inRing);
  if (!geom) return false;
  if (geom.type === 'Polygon') return inPoly(geom.coordinates);
  if (geom.type === 'MultiPolygon') return geom.coordinates.some(inPoly);
  return false;
}

/** Expand a [w, s, e, n] view box by a fraction so panning shows ships at the edges at once. */
export function padBounds(b, frac = 0.15) {
  let [w, s, e, n] = b;
  if (e - w >= 359 || (w === -180 && e === 180)) return [-180, Math.max(-85, s), 180, Math.min(85, n)];
  const width = e >= w ? e - w : e + 360 - w;
  const dx = width * frac;
  const dy = (n - s) * frac;
  if (width + 2 * dx >= 360) return [-180, Math.max(-85, s - dy), 180, Math.min(85, n + dy)];
  const wrap = (x) => ((x + 540) % 360) - 180;
  return [+wrap(w - dx).toFixed(4), +Math.max(-85, s - dy).toFixed(4), +wrap(e + dx).toFixed(4), +Math.min(85, n + dy).toFixed(4)];
}
