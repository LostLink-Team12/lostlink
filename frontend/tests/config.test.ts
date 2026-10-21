import { expect, it, vi } from 'vitest';
import { loadConfig, devProxy } from '../src/config';

it('uses public runtime settings over build settings without rebuilding', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ api: { item: '/gateway/items' }, auth: { domain: 'https://login.example', ownerClientId: 'runtime-owner' } })));
  const config = await loadConfig({ VITE_ITEM_API_URL: 'https://old.example', VITE_CLAIM_API_URL: 'https://claims.example', VITE_OWNER_CLIENT_ID: 'old-owner' }, fetcher);
  expect(config.api.item).toBe('/gateway/items');
  expect(config.api.claim).toBe('https://claims.example');
  expect(config.auth.ownerClientId).toBe('runtime-owner');
  expect(fetcher.mock.calls[0][1]).toMatchObject({ cache: 'no-store', credentials: 'omit' });
});

it('allows an absent optional runtime file but reports invalid config', async () => {
  expect((await loadConfig({ VITE_ITEM_API_URL: '/api/item' }, async () => new Response(null, { status: 404 }))).api.item).toBe('/api/item');
  await expect(loadConfig({}, async () => new Response('<html>wrong route</html>'))).rejects.toThrow(/config.json/);
  await expect(loadConfig({}, async () => new Response(JSON.stringify({ auth: { domain: 'http://remote.example' } })))).rejects.toThrow(/HTTPS/);
});

it('maps configurable local proxy prefixes to each backend without changing business paths', () => {
  const proxy = devProxy({ DEV_ITEM_API_TARGET: 'http://127.0.0.1:8103' });
  expect(proxy['/api/item'].target).toBe('http://127.0.0.1:8103');
  expect(proxy['/api/item'].rewrite('/api/item/v1/items?limit=20')).toBe('/v1/items?limit=20');
  expect(proxy).not.toHaveProperty('/api/claim');
});
