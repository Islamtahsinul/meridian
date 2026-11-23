// Shared look of the flights module: class colours, flags and small formatting helpers.

export const GROUP = '#e05aa8';
export const RED = '#ff5d52';
export const AMBER = '#ffb547';
export const GREEN = '#5fd38d';
export const BLUE = '#6cb6ff';
export const INK = '#111a2c';

export const CLASSES = {
  airliner: { color: '#e05aa8', label: 'Airliner' },
  cargo: { color: '#eba15e', label: 'Cargo' },
  business: { color: '#b9a5ff', label: 'Business jet' },
  military: { color: '#a8c467', label: 'Military' },
  helicopter: { color: '#5fcfc6', label: 'Helicopter' },
  light: { color: '#eed36c', label: 'Light aircraft' },
  glider: { color: '#91b9ee', label: 'Glider, balloon or drone' },
  ground: { color: '#8b97b0', label: 'Ground vehicle' },
  unknown: { color: '#d6d0c0', label: 'Unknown' },
};

export const FLAGS = { EST: 1, MIL: 2, EMG: 4, SANC: 8, GND: 16, MLAT: 32, PRIV: 64, LOCAL: 128 };

export const EMERGENCY_LABELS = {
  7500: 'unlawful interference', 7600: 'radio failure', 7700: 'general emergency',
  general: 'general emergency', minfuel: 'minimum fuel', nordo: 'radio failure',
  unlawful: 'unlawful interference', downed: 'aircraft downed',
};

export function classColor(cls) {
  return (CLASSES[cls] || CLASSES.unknown).color;
}

export function classLabel(cls) {
  return (CLASSES[cls] || CLASSES.unknown).label;
}

export function altText(fmt, alt, onGround) {
  if (onGround) return 'on the ground';
  if (alt == null) return null;
  return `${fmt.int(alt)} ft`;
}

// Database descriptions and owners often arrive in capitals ("AIRBUS A-320"). Show them in
// normal case without mangling model codes ("A-320", "BD-700") or real acronyms.
const KEEP = new Set(['ATR', 'BAE', 'DHC', 'CASA', 'EADS', 'IAI', 'PZL', 'MBB', 'BAC', 'HAL', 'KAI', 'NAMC', 'SIAI',
  'AIDC', 'CAC', 'EMB', 'ERJ', 'CRJ', 'II', 'III', 'IV', 'NG', 'XLS', 'XL', 'ER', 'LR', 'SP', 'SR', 'LX', 'GT', 'MD', 'SA',
  'LLC', 'LTD', 'PLC', 'GMBH', 'AG', 'NV', 'BV', 'AB', 'NA', 'USA', 'UK', 'UAE', 'RAF', 'USAF', 'NATO', 'TBM', 'SAS']);
const SMALL = new Set(['OF', 'AND', 'THE', 'DE', 'DU', 'LA', 'LE', 'DEL', 'DA', 'VON', 'VAN', 'FOR', 'Y', 'E']);

export function pretty(text) {
  if (!text || typeof text !== 'string') return text || '';
  if (/[a-z]/.test(text) && !/\b[A-Z]{4,}\b/.test(text)) return text;   // already mixed case
  return text.split(/(\s+)/).map((word, i) => {
    if (!/^[A-Z]{2,}[,.]?$/.test(word)) return word;                    // codes, numbers, mixed words
    const bare = word.replace(/[,.]$/, '');
    if (KEEP.has(bare)) return word;
    if (i > 0 && SMALL.has(bare)) return word.toLowerCase();
    return word.charAt(0) + word.slice(1).toLowerCase();
  }).join('');
}

export function titleOf(row) {
  return row.cs || row.reg || row.hex.toUpperCase();
}

export function emergencyText(code) {
  if (!code) return null;
  const label = EMERGENCY_LABELS[code];
  return /^\d{4}$/.test(code) ? `squawk ${code}${label ? `, ${label}` : ''}` : `emergency status, ${label || 'unspecified'}`;
}

const EMERGENCY_SQUAWKS = new Set(['7500', '7600', '7700']);

/** Emergency wording for a map row (rows carry the squawk, not the ADS-B status field). */
export function rowEmergencyText(row) {
  return EMERGENCY_SQUAWKS.has(row.sq) ? emergencyText(row.sq) : 'emergency status reported';
}

export function rowEmergencyShort(row) {
  return EMERGENCY_SQUAWKS.has(row.sq) ? `squawk ${row.sq}` : 'emergency status';
}

// ------------------------------------------------------------------ airspace helpers
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad2 = n => String(n).padStart(2, '0');

/** "24 Nov 23:00" in UTC (epoch seconds), or null. */
export function utcShort(t) {
  if (t == null || !Number.isFinite(+t)) return null;
  const d = new Date(+t * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}

/** "23:00" today (UTC), otherwise "24 Nov 23:00". */
export function utcClock(t, now = new Date()) {
  if (t == null || !Number.isFinite(+t)) return null;
  const d = new Date(+t * 1000);
  if (d.getUTCFullYear() === now.getUTCFullYear() && d.getUTCMonth() === now.getUTCMonth() && d.getUTCDate() === now.getUTCDate()) {
    return `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
  }
  return utcShort(t);
}

/** "31 Jan 2027" (UTC date), or null. */
export function dateShort(t) {
  if (t == null || !Number.isFinite(+t)) return null;
  const d = new Date(+t * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** 80 -> "1 h 20 min", 30 -> "30 min". */
export function minutesText(n) {
  if (n == null || !Number.isFinite(+n)) return null;
  const m = Math.round(+n);
  if (m < 60) return `${m} min`;
  return m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m / 60} h`;
}

/** Paragraphs of a multi-line official text as <p> elements inside one wrapper. */
export function prose(h, text, { max = 0, cls = 'flights-prose' } = {}) {
  let s = String(text || '').trim();
  if (max && s.length > max) s = `${s.slice(0, max).replace(/\s+\S*$/, '')}…`;
  const paras = s.split(/\n+/).map(p => p.trim()).filter(Boolean);
  return h('div', { class: cls }, paras.map(p => h('p', null, p)));
}
