// Same-origin API client (CONTRACT.md section 10.4). POSTs carry X-Meridian: 1 (the server's CSRF guard).

export class ApiError extends Error {
  constructor(status, code, message, retryAfter) {
    super(message || `HTTP ${status}`);
    this.status = status;
    this.code = code || 'HTTP_' + status;
    this.retryAfter = retryAfter;
  }
}

/** Absolute URL for an API path (MapLibre workers need absolute URLs). */
export function url(path, params) {
  const u = new URL(path, location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v == null || v === '') continue;
      u.searchParams.set(k, Array.isArray(v) ? v.join(',') : String(v));
    }
  }
  return u.toString();
}

function withTimeout(signal, ms) {
  const timeout = AbortSignal.timeout(ms);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

async function parse(res) {
  const type = res.headers.get('content-type') || '';
  let body = null;
  if (type.includes('json')) {
    try { body = await res.json(); } catch { body = null; }
  } else {
    body = await res.text();
  }
  if (!res.ok) {
    const e = body && body.error ? body.error : {};
    throw new ApiError(res.status, e.code, e.message || (typeof body === 'string' ? body.slice(0, 200) : ''), e.retry_after);
  }
  return body;
}

export async function get(path, params, { signal, timeoutMs = 30000 } = {}) {
  const res = await fetch(url(path, params), { signal: withTimeout(signal, timeoutMs), credentials: 'same-origin' });
  return parse(res);
}

export async function post(path, body, { signal, timeoutMs = 90000 } = {}) {
  const res = await fetch(url(path), {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Meridian': '1' },
    body: JSON.stringify(body ?? {}),
    signal: withTimeout(signal, timeoutMs),
  });
  return parse(res);
}

export const api = { get, post, url, ApiError };
