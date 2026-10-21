import { describe, it, expect, vi } from 'vitest';
import { createApi, uploadPhoto, waitOperation, registerItem, validatePhoto, photoBase64 } from '../src/api.js';

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
describe('backend contracts', () => {
  it('reports an unconfigured service before sending credentials to an unintended route', async () => {
    const fetcher = vi.fn();
    const api = createApi({ item: '' }, () => 'access', fetcher);
    await expect(api('item', '/v1/items')).rejects.toMatchObject({ code: 'configuration_missing' });
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('reuses an idempotency key after an uncertain network failure', async () => {
    const fetcher = vi.fn().mockRejectedValueOnce(Error('Network lost')).mockResolvedValueOnce(json({ id: 'created' }));
    const api = createApi({ item: '/api/item' }, () => 'access', fetcher);
    const options = { body: { title: 'Bag' } };
    await expect(api('item', '/v1/items', options)).rejects.toThrow('Network lost');
    await api('item', '/v1/items', options);
    expect(fetcher.mock.calls[0][1].headers['Idempotency-Key']).toBe(fetcher.mock.calls[1][1].headers['Idempotency-Key']);
  });
  it('validates photo size/type and sends raw base64 without data URL prefix', async () => {
    expect(() => validatePhoto(new File(['x'], 'file.txt', { type: 'text/plain' }))).toThrow(/JPEG/);
    expect(() => validatePhoto({ type: 'image/jpeg', size: 5 * 1024 * 1024 + 1 })).toThrow(/5 MiB/);
    expect(await photoBase64(new File(['photo'], 'bag.jpg', { type: 'image/jpeg' }))).toBe(btoa('photo'));
  });
  it('resumes confirmation after failure without recreating the item or reuploading', async () => {
    const calls = [], progress = {};
    let confirmations = 0;
    const api = async (_service, path) => {
      calls.push(path);
      if (path === '/v1/items') return { id: 'item', version: 1 };
      if (path.endsWith('/uploads')) return { upload_id: 'up', upload: { url: 'https://bucket.s3.test', fields: { key: 'photo' } } };
      if (path.endsWith('/confirm')) { if (++confirmations === 1) throw Error('Network lost'); return { operation_id: 'op' }; }
      return { state: 'completed' };
    };
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetcher);
    try {
      const file = new File(['photo'], 'bag.jpg', { type: 'image/jpeg' });
      await expect(registerItem(api, { title: 'Bag' }, file, progress)).rejects.toThrow('Network lost');
      await registerItem(api, { title: 'Bag' }, file, progress);
      expect(calls.filter(path => path === '/v1/items')).toHaveLength(1);
      expect(fetcher).toHaveBeenCalledTimes(1);
    } finally { vi.unstubAllGlobals(); }
  });
  it('uses access tokens, stable replay keys and quoted versions, never tenant headers', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ id: 'x' }));
    const api = createApi({ item: 'https://api.test' }, () => 'access', fetcher);
    await api('item', '/v1/items/x/uploads', { body: { size: 12 }, version: 3, key: 'stable-key' });
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe('https://api.test/v1/items/x/uploads');
    expect(init.headers).toMatchObject({ Authorization: 'Bearer access', 'Idempotency-Key': 'stable-key', 'If-Match': '"3"' });
    expect(init.headers).not.toHaveProperty('X-Tenant-ID');
  });
  it('retains structured errors for stale versions and expired sessions', async () => {
    const api = createApi({ item: '/api/item' }, () => 'a', vi.fn().mockResolvedValue(json({ error: { code: 'stale_version', message: 'Reload item' } }, 412)));
    await expect(api('item', '/v1/items')).rejects.toMatchObject({ status: 412, code: 'stale_version' });
  });
  it('posts the exact S3 fields and file without forwarding bearer credentials', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    const file = new File(['photo'], 'bag.jpg', { type: 'image/jpeg' });
    await uploadPhoto({ url: 'https://bucket.s3.amazonaws.com', fields: { key: 'staging/photo', policy: 'signed' } }, file, fetcher);
    const init = fetcher.mock.calls[0][1];
    expect(init.headers).toBeUndefined();
    expect(init.body.get('key')).toBe('staging/photo');
    expect(init.body.get('file')).toBe(file);
  });
  it('polls past accepted and leased, rejects failure and timeout', async () => {
    const api = vi.fn().mockResolvedValueOnce({ state: 'pending' }).mockResolvedValueOnce({ state: 'completed' });
    await expect(waitOperation(api, 'item', 'op', { delay: async () => {}, attempts: 2 })).resolves.toEqual({ state: 'completed' });
    await expect(waitOperation(async () => ({ state: 'needs_attention' }), 'claim', 'op')).rejects.toThrow(/needs_attention/);
    await expect(waitOperation(async () => ({ state: 'pending' }), 'item', 'op', { delay: async () => {}, attempts: 1 })).rejects.toThrow(/pending/);
  });
});
