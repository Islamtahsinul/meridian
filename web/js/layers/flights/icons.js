// Aircraft silhouettes drawn on canvas (nose up, so icon-rotate = track) and converted into signed
// distance fields, so MapLibre can tint them per class (icon-color) and draw a crisp ink halo or a
// hollow outline for estimated positions (icon-halo-*). Shapes are original, simple and top-down.

const BOX = 48;          // CSS px per icon (the core canvas uses pixelRatio 2, so 96 device px)
const C = BOX / 2;
const S = 20;            // unit -> px: shapes live in [-1, 1]
const SDF_RADIUS = 8;    // device px
const SDF_CUTOFF = 0.25; // edge at 0.75, as MapLibre's SDF shader expects

function pt(x, y) { return [C + x * S, C + y * S]; }

/** Symmetric polygon from its right half (x >= 0), listed top to bottom. */
function sym(g, half) {
  const pts = [...half, ...half.slice().reverse().map(([x, y]) => [-x, y])];
  g.beginPath();
  pts.forEach(([x, y], i) => { const [px, py] = pt(x, y); if (i) g.lineTo(px, py); else g.moveTo(px, py); });
  g.closePath();
  g.fill();
}

function capsule(g, y0, y1, hw, x = 0) {
  const r = hw * S;
  const px = C + x * S;
  const top = C + y0 * S + r;
  const bottom = C + y1 * S - r;
  g.beginPath();
  g.moveTo(px - r, top);
  g.arc(px, top, r, Math.PI, 0);
  g.lineTo(px + r, bottom);
  g.arc(px, bottom, r, 0, Math.PI);
  g.closePath();
  g.fill();
}

function ellipse(g, x, y, rx, ry) {
  g.beginPath();
  g.ellipse(C + x * S, C + y * S, rx * S, ry * S, 0, 0, Math.PI * 2);
  g.fill();
}

function rect(g, x0, y0, x1, y1) {
  g.fillRect(C + x0 * S, C + y0 * S, (x1 - x0) * S, (y1 - y0) * S);
}

function roundRect(g, x0, y0, x1, y1, r) {
  const [a, b] = pt(x0, y0);
  const [c, d] = pt(x1, y1);
  const rr = r * S;
  g.beginPath();
  g.moveTo(a + rr, b);
  g.arcTo(c, b, c, d, rr);
  g.arcTo(c, d, a, d, rr);
  g.arcTo(a, d, a, b, rr);
  g.arcTo(a, b, c, b, rr);
  g.closePath();
  g.fill();
}

function bar(g, x, y, len, w, angle) {
  g.save();
  g.translate(C + x * S, C + y * S);
  g.rotate(angle);
  g.fillRect(-len * S / 2, -w * S / 2, len * S, w * S);
  g.restore();
}

const SHAPES = {
  jet(g) {
    capsule(g, -0.98, 0.94, 0.085);
    sym(g, [[0.06, -0.24], [0.97, 0.17], [0.97, 0.28], [0.06, 0.12]]);
    ellipse(g, 0.38, -0.05, 0.058, 0.13);
    ellipse(g, -0.38, -0.05, 0.058, 0.13);
    sym(g, [[0.05, 0.62], [0.39, 0.84], [0.39, 0.93], [0.05, 0.85]]);
  },
  heavy(g) {
    capsule(g, -1.0, 0.97, 0.12);
    sym(g, [[0.08, -0.26], [1.0, 0.18], [1.0, 0.3], [0.08, 0.15]]);
    ellipse(g, 0.36, -0.08, 0.075, 0.15);
    ellipse(g, -0.36, -0.08, 0.075, 0.15);
    ellipse(g, 0.66, 0.06, 0.06, 0.12);
    ellipse(g, -0.66, 0.06, 0.06, 0.12);
    sym(g, [[0.07, 0.6], [0.45, 0.85], [0.45, 0.95], [0.07, 0.87]]);
  },
  turboprop(g) {
    capsule(g, -0.92, 0.95, 0.085);
    sym(g, [[0.06, -0.2], [0.98, -0.13], [0.98, -0.02], [0.06, 0.05]]);
    capsule(g, -0.44, 0.12, 0.062, 0.34);
    capsule(g, -0.44, 0.12, 0.062, -0.34);
    rect(g, 0.14, -0.5, 0.54, -0.45);
    rect(g, -0.54, -0.5, -0.14, -0.45);
    sym(g, [[0.05, 0.7], [0.37, 0.74], [0.37, 0.86], [0.05, 0.87]]);
  },
  bizjet(g) {
    capsule(g, -0.97, 0.86, 0.075);
    sym(g, [[0.05, -0.08], [0.84, 0.28], [0.84, 0.37], [0.05, 0.2]]);
    ellipse(g, 0.18, 0.5, 0.065, 0.15);
    ellipse(g, -0.18, 0.5, 0.065, 0.15);
    sym(g, [[0.04, 0.74], [0.38, 0.9], [0.38, 0.98], [0.04, 0.93]]);
  },
  prop(g) {
    capsule(g, -0.8, 0.93, 0.08);
    rect(g, -0.26, -0.88, 0.26, -0.82);
    sym(g, [[0.05, -0.38], [0.98, -0.35], [0.98, -0.17], [0.05, -0.15]]);
    sym(g, [[0.04, 0.7], [0.38, 0.72], [0.38, 0.85], [0.04, 0.87]]);
  },
  heli(g) {
    ellipse(g, 0, -0.1, 0.22, 0.34);
    capsule(g, 0.12, 0.96, 0.05);
    rect(g, -0.2, 0.84, 0.2, 0.9);
    bar(g, 0, -0.1, 1.95, 0.075, Math.PI / 4);
    bar(g, 0, -0.1, 1.95, 0.075, -Math.PI / 4);
  },
  fighter(g) {
    sym(g, [[0, -1.0], [0.07, -0.72], [0.1, -0.2], [0.1, 0.86], [0.06, 0.97]]);
    sym(g, [[0.08, -0.24], [0.8, 0.36], [0.8, 0.47], [0.08, 0.5]]);
    sym(g, [[0.07, 0.62], [0.38, 0.86], [0.38, 0.95], [0.07, 0.93]]);
  },
  glider(g) {
    capsule(g, -0.72, 0.94, 0.055);
    sym(g, [[0.04, -0.25], [1.0, -0.2], [1.0, -0.12], [0.04, -0.1]]);
    sym(g, [[0.03, 0.84], [0.28, 0.85], [0.28, 0.92], [0.03, 0.94]]);
  },
  balloon(g) {
    ellipse(g, 0, -0.22, 0.6, 0.64);
    sym(g, [[0.3, 0.32], [0.12, 0.66]]);
    roundRect(g, -0.15, 0.64, 0.15, 0.9, 0.04);
  },
  uav(g) {
    capsule(g, -0.62, 0.74, 0.075);
    sym(g, [[0.05, -0.14], [1.0, -0.1], [1.0, 0.0], [0.05, 0.05]]);
    sym(g, [[0.04, 0.5], [0.4, 0.8], [0.34, 0.88], [0.03, 0.66]]);
    rect(g, -0.17, 0.76, 0.17, 0.81);
  },
  ground(g) {
    roundRect(g, -0.4, -0.66, 0.4, 0.66, 0.16);
    g.save();
    g.globalCompositeOperation = 'destination-out';
    roundRect(g, -0.26, -0.46, 0.26, -0.26, 0.05);
    g.restore();
  },
  generic(g) {
    sym(g, [[0, -0.96], [0.66, 0.84], [0, 0.46]]);
  },
};

export const SHAPE_NAMES = Object.keys(SHAPES);

// ------------------------------------------------------------------ signed distance field
const INF = 1e20;

function edt1d(grid, offset, stride, length, f, v, z) {
  for (let q = 0; q < length; q++) f[q] = grid[offset + q * stride];
  v[0] = 0;
  z[0] = -INF;
  z[1] = INF;
  for (let q = 1, k = 0; q < length; q++) {
    let s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
    while (s <= z[k]) {
      k--;
      s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
    }
    k++;
    v[k] = q;
    z[k] = s;
    z[k + 1] = INF;
  }
  for (let q = 0, k = 0; q < length; q++) {
    while (z[k + 1] < q) k++;
    const d = q - v[k];
    grid[offset + q * stride] = d * d + f[v[k]];
  }
}

function edt(grid, w, h) {
  const n = Math.max(w, h);
  const f = new Float64Array(n);
  const v = new Uint16Array(n);
  const z = new Float64Array(n + 1);
  for (let x = 0; x < w; x++) edt1d(grid, x, w, h, f, v, z);
  for (let y = 0; y < h; y++) edt1d(grid, y * w, 1, w, f, v, z);
}

/** Turn an anti-aliased alpha mask (RGBA bytes) into an SDF in place: edge = 191, inside 255. */
export function toSdf(data, w, h, radius = SDF_RADIUS, cutoff = SDF_CUTOFF) {
  const size = w * h;
  const outer = new Float64Array(size);
  const inner = new Float64Array(size);
  for (let i = 0; i < size; i++) {
    const a = data[i * 4 + 3] / 255;
    if (a >= 1) { outer[i] = 0; inner[i] = INF; }
    else if (a <= 0) { outer[i] = INF; inner[i] = 0; }
    else {
      const d = 0.5 - a;
      outer[i] = d > 0 ? d * d : 0;
      inner[i] = d < 0 ? d * d : 0;
    }
  }
  edt(outer, w, h);
  edt(inner, w, h);
  for (let i = 0; i < size; i++) {
    const dist = Math.sqrt(outer[i]) - Math.sqrt(inner[i]);
    const value = Math.round(255 - 255 * (dist / radius + cutoff));
    data[i * 4] = 255;
    data[i * 4 + 1] = 255;
    data[i * 4 + 2] = 255;
    data[i * 4 + 3] = value < 0 ? 0 : value > 255 ? 255 : value;
  }
}

function drawer(name) {
  return (g) => {
    // Draw on a private canvas meant for read-back, then hand the core's canvas the finished SDF
    // (the core reads its canvas exactly once).
    const w = g.canvas.width;
    const h = g.canvas.height;
    const own = document.createElement('canvas');
    own.width = w;
    own.height = h;
    const o = own.getContext('2d', { willReadFrequently: true });
    o.scale(w / BOX, h / BOX);
    o.fillStyle = '#fff';
    SHAPES[name](o);
    const img = o.getImageData(0, 0, w, h);
    toSdf(img.data, w, h);
    g.putImageData(img, 0, 0);
  };
}

/** Register every silhouette as an SDF icon named flights-<shape>. */
export function registerIcons(map2) {
  for (const name of SHAPE_NAMES) map2.icon(`flights-${name}`, drawer(name), { size: BOX, sdf: true, pixelRatio: 2 });
}

/** A small inline preview (canvas) of a silhouette, for legends and cards. */
export function previewCanvas(name, color, px = 18) {
  const canvas = document.createElement('canvas');
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(px * ratio);
  canvas.height = Math.round(px * ratio);
  canvas.style.width = `${px}px`;
  canvas.style.height = `${px}px`;
  const g = canvas.getContext('2d');
  g.scale(canvas.width / BOX, canvas.height / BOX);
  g.fillStyle = color;
  (SHAPES[name] || SHAPES.generic)(g);
  canvas.setAttribute('aria-hidden', 'true');
  return canvas;
}
