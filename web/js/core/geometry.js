// Pure geometry for drawing (CONTRACT.md section 15.3): circles, rectangles and polygons as GeoJSON,
// longitude unwrapping, areas and point-in-shape tests. No DOM or MapLibre here, so Node can test it
// (tests/test_core_draw.mjs).
//
// Shapes keep continuous longitudes: one that crosses the antimeridian may have vertices beyond ±180
// (e.g. 175 to 185), shifted so its first vertex or centre lies within ±180. Its bbox is wrapped into
// ±180, with west > east when it crosses. pointInShape() handles both.

export const EARTH_RADIUS_KM = 6371.0088;
export const MAX_RADIUS_KM = 5000;
const RAD = Math.PI / 180;

export const round5 = (x) => Math.round(x * 1e5) / 1e5;

/** Longitude into [-180, 180). */
export function wrapLon(lon) {
  return ((((lon + 180) % 360) + 360) % 360) - 180;
}

/** lon shifted by a multiple of 360 to lie within 180 degrees of ref. */
export function unwrapNear(lon, ref) {
  let x = lon;
  while (x - ref > 180) x -= 360;
  while (x - ref < -180) x += 360;
  return x;
}

/** [[lon, lat], ...] with each longitude within 180 degrees of the previous one, then shifted so the
 *  first point lies within ±180. */
export function unwrapPath(points) {
  const out = [];
  for (const [lon, lat] of points) {
    out.push([out.length ? unwrapNear(lon, out[out.length - 1][0]) : lon, lat]);
  }
  if (out.length && Math.abs(out[0][0]) > 180) {
    const shift = 360 * Math.round(out[0][0] / 360);
    for (const p of out) p[0] -= shift;
  }
  return out;
}

export function haversineKm(lat1, lon1, lat2, lon2) {
  const dp = (lat2 - lat1) * RAD;
  const dl = (lon2 - lon1) * RAD;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(lat1 * RAD) * Math.cos(lat2 * RAD) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** [lon, lat] reached from (lat, lon) after distKm on the initial bearing (degrees from north). */
export function destination(lat, lon, bearingDeg, distKm) {
  const d = distKm / EARTH_RADIUS_KM;
  const b = bearingDeg * RAD;
  const p1 = lat * RAD;
  const l1 = lon * RAD;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [wrapLon(l2 / RAD), p2 / RAD];
}

/** Largest radius for a circle at this latitude: it must not reach a pole (a ring can't enclose one). */
export function maxRadiusKm(lat) {
  const toPole = (90 - Math.abs(lat)) * RAD * EARTH_RADIUS_KM;
  return Math.max(0, Math.min(MAX_RADIUS_KM, toPole - 1));
}

/** Planar signed area in degrees² (positive = counter-clockwise with x = lon, y = lat). */
export function signedArea(ring) {
  let s = 0;
  for (let i = 0, n = ring.length; i < n; i++) {
    const [x1, y1] = ring[i];
    const [x2, y2] = ring[(i + 1) % n];
    s += x1 * y2 - x2 * y1;
  }
  return s / 2;
}

/** Area of a closed ring on the sphere, km² (Chamberlain and Duquette, JPL 2007). */
export function ringAreaKm2(ring) {
  const n = ring.length;
  if (n < 4) return 0;
  let total = 0;
  for (let i = 0; i < n; i++) {
    let lower, middle, upper;
    if (i === n - 2) { lower = n - 2; middle = n - 1; upper = 0; }
    else if (i === n - 1) { lower = n - 1; middle = 0; upper = 1; }
    else { lower = i; middle = i + 1; upper = i + 2; }
    total += (ring[upper][0] - ring[lower][0]) * RAD * Math.sin(ring[middle][1] * RAD);
  }
  return Math.abs(total * EARTH_RADIUS_KM * EARTH_RADIUS_KM / 2);
}

/** Round to 5 decimals, drop repeated points, close the ring and make it counter-clockwise
 *  (RFC 7946). null when fewer than three distinct points remain. */
export function cleanRing(points) {
  const ring = [];
  for (const [lon, lat] of points) {
    const p = [round5(lon), round5(Math.max(-90, Math.min(90, lat)))];
    const last = ring[ring.length - 1];
    if (!last || last[0] !== p[0] || last[1] !== p[1]) ring.push(p);
  }
  while (ring.length > 1 && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1]) ring.pop();
  if (ring.length < 3 || Math.abs(signedArea(ring)) < 1e-12) return null;
  if (signedArea(ring) < 0) ring.reverse();
  ring.push([ring[0][0], ring[0][1]]);
  return ring;
}

/** [west, south, east, north] of a ring with continuous longitudes, wrapped into ±180
 *  (west > east when it crosses the antimeridian). */
export function bboxOf(ring) {
  let w = Infinity, e = -Infinity, s = Infinity, n = -Infinity;
  for (const [lon, lat] of ring) {
    if (lon < w) w = lon;
    if (lon > e) e = lon;
    if (lat < s) s = lat;
    if (lat > n) n = lat;
  }
  if (e - w >= 360) return [-180, round5(s), 180, round5(n)];
  const ww = w < -180 || w > 180 ? wrapLon(w) : w;
  let ee = e < -180 || e > 180 ? wrapLon(e) : e;
  if (ee === -180 && e > w) ee = 180;
  return [round5(ww), round5(s), round5(ee), round5(n)];
}

function feature(ring, properties) {
  return {
    type: 'Feature',
    bbox: bboxOf(ring),
    geometry: { type: 'Polygon', coordinates: [ring] },
    properties: { ...properties, area_km2: Math.round(ringAreaKm2(ring) * 100) / 100 },
  };
}

/** A polygon from clicked [lon, lat] points (any longitudes; they are unwrapped). null if degenerate. */
export function polygonFeature(points) {
  const ring = cleanRing(unwrapPath(points));
  return ring ? feature(ring, { mode: 'polygon' }) : null;
}

/** A geodesic circle as a polygon, with properties.center [lon, lat] and properties.radius_km.
 *  The radius is clamped so the circle stays off the poles. null for a zero radius. */
export function circleFeature(center, radiusKm, steps = 72) {
  const lat = Math.max(-89.9, Math.min(89.9, center[1]));
  const lon = Math.abs(center[0]) <= 180 ? center[0] : wrapLon(center[0]);
  const r = Math.min(Math.max(0, +radiusKm || 0), maxRadiusKm(lat));
  if (!(r > 0.001)) return null;
  const pts = [];
  for (let i = 0; i < steps; i++) {
    const [x, y] = destination(lat, lon, -i * 360 / steps, r);   // anticlockwise from north
    pts.push([unwrapNear(x, lon), y]);
  }
  const ring = cleanRing(pts);
  if (!ring) return null;
  return feature(ring, { mode: 'circle', center: [round5(lon), round5(lat)], radius_km: Math.round(r * 1000) / 1000 });
}

/** A longitude/latitude-aligned box from two opposite corners (the shorter way round in longitude). */
export function rectangleFeature(a, b) {
  const [p, q] = unwrapPath([a, b]);
  const w = Math.min(p[0], q[0]);
  const e = Math.max(p[0], q[0]);
  const s = Math.max(-90, Math.min(p[1], q[1]));
  const n = Math.min(90, Math.max(p[1], q[1]));
  const ring = cleanRing([[w, s], [e, s], [e, n], [w, n]]);
  return ring ? feature(ring, { mode: 'rectangle' }) : null;
}

/** Ray casting on one ring (x = lon, y = lat). */
export function pointInRing(lon, lat, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / ((yj - yi) || 1e-12) + xi) inside = !inside;
  }
  return inside;
}

function inPolygon(lon, lat, rings) {
  if (!rings?.length || !pointInRing(lon, lat, rings[0])) return false;
  return !rings.slice(1).some(hole => pointInRing(lon, lat, hole));
}

/** Is (lat, lon) inside a Polygon or MultiPolygon (Feature or geometry)? Longitudes beyond ±180 in
 *  the shape (antimeridian crossings) are handled. */
export function pointInShape(shape, lat, lon) {
  const g = shape?.type === 'Feature' ? shape.geometry : shape;
  if (!g || !Array.isArray(g.coordinates) || !Number.isFinite(+lat) || !Number.isFinite(+lon)) return false;
  const polys = g.type === 'Polygon' ? [g.coordinates] : g.type === 'MultiPolygon' ? g.coordinates : [];
  const x = wrapLon(+lon);
  for (const rings of polys) {
    for (const cand of [x, x + 360, x - 360]) if (inPolygon(cand, +lat, rings)) return true;
  }
  return false;
}

/** A point to hang a label on: the circle's centre, else the mean of the outer ring's vertices. */
export function labelPoint(shape) {
  const g = shape?.type === 'Feature' ? shape.geometry : shape;
  const c = shape?.properties?.center;
  if (Array.isArray(c) && c.length === 2) return [+c[0], +c[1]];
  const ring = g?.type === 'Polygon' ? g.coordinates[0] : g?.type === 'MultiPolygon' ? g.coordinates[0]?.[0] : null;
  if (!ring?.length) return null;
  const pts = ring.length > 1 ? ring.slice(0, -1) : ring;
  let x = 0, y = 0;
  for (const [lon, lat] of pts) { x += lon; y += lat; }
  return [x / pts.length, y / pts.length];
}
