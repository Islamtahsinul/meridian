// DOM and formatting helpers (CONTRACT.md section 10.2). Never build HTML strings from data:
// use h() so every value becomes a text node or an attribute.

const SAFE_URL = /^(https?:|blob:|data:image\/|\/(?![\/\\])|\.\/|#)/i;
const UNSAFE_CHARS = /[\\\u0000-\u001f\u007f]/;   // backslashes and control characters (tab, newline)

export function safeUrl(url) {
  const s = String(url ?? '').trim();
  if (UNSAFE_CHARS.test(s)) return null;
  return SAFE_URL.test(s) ? s : null;
}

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** h('div', {class: 'x', onClick: fn, style: {color: 'red'}}, 'text', childNode, [more]) */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') {
        for (const [sk, sv] of Object.entries(v)) {
          if (sv == null) continue;
          if (sk.startsWith('--')) el.style.setProperty(sk, sv); else el.style[sk] = sv;
        }
      } else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'href' || k === 'src' || k === 'poster') {
        const u = safeUrl(v);
        if (u) el.setAttribute(k, u);
      } else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  if (tag === 'a' && el.getAttribute('href') && /^https?:/i.test(el.getAttribute('href'))) {
    el.setAttribute('target', '_blank');
    el.setAttribute('rel', 'noopener noreferrer');
  }
  return el;
}

export function append(el, children) {
  for (const c of [children].flat(Infinity)) {
    if (c == null || c === false || c === '') continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const KIND_LABELS = {
  aircraft: 'Aircraft', ship: 'Ship', camera: 'Camera', airport: 'Airport', port: 'Port', power_plant: 'Power plant',
  dam: 'Dam', datacenter: 'Data centre', cable: 'Submarine cable', landing_point: 'Cable landing point', company: 'Company',
  instrument: 'Market', earthquake: 'Earthquake', fire: 'Fire', volcano: 'Volcano', storm: 'Tropical storm', flood: 'Flood',
  warning: 'Weather warning', outbreak: 'Disease outbreak', disaster: 'Disaster alert', tsunami: 'Tsunami message',
  conflict_event: 'Conflict event', frontline: 'Front line', news: 'News', risk_zone: 'Risk zone', country: 'Country',
  convergence: 'Convergence', cyber: 'Cyber', outage: 'Internet outage', satellite: 'Satellite', launch: 'Launch',
  launch_pad: 'Launch pad', strait: 'Strait', gps_jam: 'GPS interference', sanctioned_vessel: 'Sanctioned vessel',
  sanctioned_aircraft: 'Sanctioned aircraft', place: 'Place', point: 'Point',
  space_weather: 'Space weather', airport_delay: 'Airport delay', flight_restriction: 'Flight restriction',
  airspace_advisory: 'Airspace advisory', protest: 'Protest', news_spike: 'News spike', alert: 'Alert',
  emergency_mapping: 'Mapping activation', gauge: 'Water level gauge', advice_change: 'Travel advice change',
  air_activity: 'Air activity',
};

/** Sentence-case name for an entity kind ("power_plant" -> "Power plant"). */
export function kindLabel(kind, plural = false) {
  const base = KIND_LABELS[kind] || String(kind || 'Other').replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
  if (!plural) return base;
  if (/(News|Cyber|GPS interference|Aircraft)$/.test(base)) return base;
  return /(s|x|ch|sh|o)$/.test(base) ? `${base}es` : base.endsWith('y') && !/[aeiou]y$/.test(base) ? `${base.slice(0, -1)}ies` : `${base}s`;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

/** Inline SVG from a small, trusted, hard-coded path string (never from data). */
export function svgIcon(pathD, { size = 18, viewBox = '0 0 20 20', stroke = true } = {}) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', viewBox);
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS(ns, 'path');
  p.setAttribute('d', pathD);
  if (stroke) {
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke', 'currentColor');
    p.setAttribute('stroke-width', '1.6');
    p.setAttribute('stroke-linecap', 'round');
    p.setAttribute('stroke-linejoin', 'round');
  } else {
    p.setAttribute('fill', 'currentColor');
  }
  svg.append(p);
  return svg;
}

// ------------------------------------------------------------------ time
/** Accepts epoch seconds, epoch milliseconds, ISO strings or Date. Returns ms or NaN. */
export function toMs(t) {
  if (t == null || t === '') return NaN;
  if (t instanceof Date) return t.getTime();
  if (typeof t === 'number') return t < 1e11 ? t * 1000 : t;
  const n = Number(t);
  if (!Number.isNaN(n) && /^\d+(\.\d+)?$/.test(String(t))) return n < 1e11 ? n * 1000 : n;
  return Date.parse(t);
}

const pad = n => String(n).padStart(2, '0');

export const fmt = {
  int(n) { return n == null || Number.isNaN(+n) ? '–' : Math.round(+n).toLocaleString('en-GB'); },
  num(n, d = 1) { return n == null || Number.isNaN(+n) ? '–' : (+n).toLocaleString('en-GB', { maximumFractionDigits: d, minimumFractionDigits: 0 }); },
  fixed(n, d = 1) { return n == null || Number.isNaN(+n) ? '–' : (+n).toFixed(d); },
  compact(n) { return n == null || Number.isNaN(+n) ? '–' : new Intl.NumberFormat('en-GB', { notation: 'compact', maximumFractionDigits: 1 }).format(+n); },
  pct(n, d = 0) { return n == null || Number.isNaN(+n) ? '–' : `${(+n).toFixed(d)}%`; },
  km(n) { return n == null || Number.isNaN(+n) ? '–' : `${fmt.num(n, +n < 10 ? 1 : 0)} km`; },
  ft(n) { return n == null || Number.isNaN(+n) ? '–' : `${fmt.int(n)} ft`; },
  kt(n) { return n == null || Number.isNaN(+n) ? '–' : `${fmt.int(n)} kn`; },
  deg(n) { return n == null || Number.isNaN(+n) ? '–' : `${Math.round(+n) % 360}°`; },
  money(n, cur = 'USD') {
    if (n == null || Number.isNaN(+n)) return '–';
    return new Intl.NumberFormat('en-GB', { style: 'currency', currency: cur, notation: Math.abs(+n) >= 1e6 ? 'compact' : 'standard', maximumFractionDigits: Math.abs(+n) >= 1e6 ? 1 : 2 }).format(+n);
  },
  latlon(lat, lon, d = 4) {
    if (lat == null || lon == null) return '–';
    const ns = lat >= 0 ? 'N' : 'S';
    const ew = lon >= 0 ? 'E' : 'W';
    return `${Math.abs(lat).toFixed(d)}° ${ns}, ${Math.abs(lon).toFixed(d)}° ${ew}`;
  },
  /** "2026-11-22 14:05 UTC" */
  utc(t) {
    const ms = toMs(t);
    if (Number.isNaN(ms)) return '–';
    const d = new Date(ms);
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
  },
  /** "14:05:09" UTC */
  clock(t) {
    const ms = toMs(t);
    if (Number.isNaN(ms)) return '–';
    const d = new Date(ms);
    return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  },
  /** "4 min ago", "in 2 h" */
  ago(t) {
    const ms = toMs(t);
    if (Number.isNaN(ms)) return '–';
    let s = Math.round((Date.now() - ms) / 1000);
    const future = s < 0;
    s = Math.abs(s);
    let out;
    if (s < 45) out = `${s} s`;
    else if (s < 3600) out = `${Math.round(s / 60)} min`;
    else if (s < 86400 * 2) out = `${Math.round(s / 3600)} h`;
    else if (s < 86400 * 60) out = `${Math.round(s / 86400)} days`;
    else out = `${Math.round(s / (86400 * 30))} months`;
    return future ? `in ${out}` : `${out} ago`;
  },
};

export function debounce(fn, ms) {
  let t = null;
  const d = (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  d.cancel = () => clearTimeout(t);
  return d;
}

export function throttle(fn, ms) {
  let last = 0;
  let timer = null;
  let lastArgs = null;
  return (...args) => {
    lastArgs = args;
    const now = performance.now();
    const wait = ms - (now - last);
    if (wait <= 0) {
      last = now;
      fn(...args);
    } else if (!timer) {
      timer = setTimeout(() => { timer = null; last = performance.now(); fn(...lastArgs); }, wait);
    }
  };
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function download(filename, text, type = 'application/json') {
  const blob = new Blob([text], { type });
  const a = h('a', { href: URL.createObjectURL(blob), download: filename });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

export function validLatLon(lat, lon) {
  return Number.isFinite(+lat) && Number.isFinite(+lon) && Math.abs(+lat) <= 90 && Math.abs(+lon) <= 180 && lat !== null && lon !== null && lat !== '' && lon !== '';
}

export function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371.0088;
  const r = Math.PI / 180;
  const dp = (lat2 - lat1) * r;
  const dl = (lon2 - lon1) * r;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}
