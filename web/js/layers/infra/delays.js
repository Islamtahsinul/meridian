// FAA status for the airport card, from the flights module's GET /api/flights/airport-status/{ident}
// (CONTRACT 9.1). Pure helpers: they take ctx.h and ctx.fmt, so tests can run them without a browser.
//
// The response's top-level fields describe only the most serious status; `items` lists every status
// (delays, programmes, closures, de-icing, notices). Each item's `updated` is when the FAA last changed
// that status, which can be months old for a long closure, so the card shows `fetched_at` as "Checked".

const TONE = { high: 'red', medium: 'amber', low: 'blue' };   // the same tones as the flights.delays card

export const plain = (v) => {
  const t = String(v ?? '').replace(/_/g, ' ').replace(/^[A-Z]{2,4}\s*:\s*/, '').trim();
  if (!t) return null;
  const out = t.split(/\s+/).map(w => (/^[A-Z0-9]{2,4}$/.test(w) ? w : w.toLowerCase())).join(' ');
  return out.charAt(0).toUpperCase() + out.slice(1);
};

/** Every status the FAA lists, most serious first (older answers without `items`: the top-level status). */
export function delayItems(d) {
  if (!d || typeof d !== 'object') return [];
  if (Array.isArray(d.items)) return d.items.filter(it => it && typeof it === 'object' && (it.type || it.delay));
  return d.type || d.delay ? [d] : [];
}

function toneFor(it) {
  if (TONE[it.level]) return TONE[it.level];
  if (/stop|clos/i.test(String(it.type || ''))) return 'red';
  return it.delay === false ? 'blue' : 'amber';
}

/** One badge for the airport card: the most serious status, delay or not (closures and notices too). */
export function delayBadge(d) {
  const top = delayItems(d)[0];
  if (!top) return null;
  const type = plain(top.type) || 'Delays';
  return { text: type.length > 28 ? 'FAA status' : type, tone: toneFor(top), title: top.summary || plain(top.reason) || null };
}

function minutes(fmt, v) {
  return v != null && v !== '' && Number.isFinite(+v) ? `${fmt.int(+v)} min` : null;
}

/** What one status says when the FAA gave no summary sentence. */
function itemText(fmt, it) {
  if (it.summary) return String(it.summary);
  const range = it.min_delay_min != null && it.max_delay_min != null ? `${fmt.int(+it.min_delay_min)} to ${fmt.int(+it.max_delay_min)} min` : null;
  const avg = minutes(fmt, it.avg_delay_min);
  const parts = [plain(it.reason), range ? `delays ${range}` : (avg ? `${avg} on average` : null), plain(it.trend)?.toLowerCase()];
  const out = parts.filter(Boolean).join(', ');
  return out ? out.charAt(0).toUpperCase() + out.slice(1) : (it.delay === false ? 'Notice, no delay' : 'Delays reported');
}

export function delaySection(d, { h, fmt }) {
  if (!d || typeof d !== 'object') return null;
  const items = delayItems(d);
  const dl = h('dl', { class: 'kv' });
  const row = (k, v, cls) => { if (v != null && v !== '') dl.append(h('dt', cls ? { class: cls } : null, k), h('dd', null, String(v))); };
  if (!items.some(it => it.delay)) row('Now', 'No delays reported');
  for (const it of items) row(plain(it.type) || 'Status', itemText(fmt, it), `infra-delay-${toneFor(it)}`);
  if (d.fetched_at) row('Checked', fmt.ago(d.fetched_at));
  else if (d.updated) row('FAA last changed', fmt.ago(d.updated));
  return h('div', { class: 'infra-delays' }, dl, h('p', { class: 'faint' }, `Source: ${plain(d.source) || 'FAA'}, through the flights module.`));
}
