export class ApiError extends Error {
  constructor(status, code, message, details = []) { super(message); this.status = status; this.code = code; this.details = details; }
}

function validationMessage(error) {
  const issues = Array.isArray(error?.details) ? error.details : [];
  if (issues.some(issue => issue?.type === 'invalid_date_range')) return 'Found from must be earlier than or equal to Found until.';
  const dateIssue = issues.find(issue => Array.isArray(issue?.location) && ['found_from', 'found_until'].includes(issue.location.at(-1)));
  if (dateIssue) return `${dateIssue.location.at(-1) === 'found_from' ? 'Found from' : 'Found until'} must be a valid date and time with a timezone.`;
  return error?.message;
}

export function createApi(bases, token, fetcher = fetch) {
  const retries = new Map();
  return async (service, path, { body, version, key, method = body === undefined ? 'GET' : 'POST' } = {}) => {
    if (!bases[service]) throw new ApiError(503, 'configuration_missing', `${service} API is not configured. Set its public URL in config.json or VITE_*_API_URL.`);
    const accessToken = token();
    if (!accessToken) throw new ApiError(401, 'login_required', 'Please sign in again.');
    const headers = { Authorization: `Bearer ${accessToken}` };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const signature = JSON.stringify([service, path, method, body, version]);
    if (method !== 'GET') {
      if (!key && !retries.has(signature)) retries.set(signature, crypto.randomUUID());
      headers['Idempotency-Key'] = key || retries.get(signature);
    }
    if (version !== undefined) headers['If-Match'] = `"${version}"`;
    const response = await fetcher((bases[service] || '').replace(/\/$/, '') + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(30000), redirect: 'error',
    });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = response.status === 422 && service === 'matching' ? validationMessage(value.error) : value.error?.message;
      throw new ApiError(response.status, value.error?.code || 'request_failed', message || `Request failed (${response.status})`, Array.isArray(value.error?.details) ? value.error.details : []);
    }
    retries.delete(signature);
    return value;
  };
}

export async function uploadPhoto(presigned, file, fetcher = fetch) {
  validatePhoto(file, 10);
  const url = new URL(presigned.url);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) throw Error('Invalid photo upload URL');
  const body = new FormData();
  for (const [name, value] of Object.entries(presigned.fields)) body.append(name, value);
  body.append('file', file);
  const response = await fetcher(url.href, { method: 'POST', body, redirect: 'error', signal: AbortSignal.timeout(60000) });
  if (!response.ok) throw Error(`Photo upload failed (${response.status})`);
}

export function validatePhoto(file, maxMiB = 5) {
  if (!file || !['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size < 1 || file.size > maxMiB * 1024 * 1024) throw Error(`Choose a JPEG, PNG or WebP photo, up to ${maxMiB} MiB.`);
}

export async function photoBase64(file) {
  validatePhoto(file);
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.onerror = () => reject(Error('Photo could not be read'));
    reader.readAsDataURL(file);
  });
}

export async function waitOperation(api, service, id, { attempts = 120, delay = () => new Promise(r => setTimeout(r, 1000)) } = {}) {
  for (let i = 0; i < attempts; i++) {
    const value = await api(service, `/v1/${service === 'item' ? 'item' : 'claim'}-operations/${encodeURIComponent(id)}`);
    if (value.state === 'completed') return value;
    if (['failed', 'superseded', 'needs_attention'].includes(value.state)) throw Error(`Operation ${id}: ${value.state}. Refresh to inspect current status.`);
    await delay();
  }
  throw Error(`Operation ${id} is still pending. Refresh to check its status.`);
}

// A single registration can be resumed after a network failure without duplicating the item.
export async function registerItem(api, item, file, progress = {}, changed = () => {}) {
  validatePhoto(file, 10);
  progress.key ||= crypto.randomUUID();
  if (!progress.item) { progress.item = await api('item', '/v1/items', { body: item, key: progress.key }); changed(progress); }
  const id = encodeURIComponent(progress.item.id);
  if (!progress.upload) {
    progress.upload = await api('item', `/v1/items/${id}/uploads`, { body: { content_type: file.type, size: file.size }, version: progress.item.version, key: `${progress.key}-upload` });
    changed(progress);
  }
  if (!progress.sent) { await uploadPhoto(progress.upload.upload, file); progress.sent = true; changed(progress); }
  if (!progress.operation) {
    progress.operation = await api('item', `/v1/items/${id}/uploads/${encodeURIComponent(progress.upload.upload_id)}/confirm`, { body: {}, key: `${progress.key}-confirm` });
    changed(progress);
  }
  await waitOperation(api, 'item', progress.operation.operation_id);
  return progress.item;
}
