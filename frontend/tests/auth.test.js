import { it, expect } from 'vitest';
import { beginLogin, finishLogin } from '../src/auth.js';

function storage() { const values = new Map(); return { getItem: k => values.get(k), setItem: (k,v) => values.set(k,v), removeItem: k => values.delete(k) }; }
const config = { domain: 'https://example.auth.ap-southeast-1.amazoncognito.com', clientId: 'public-client', redirectUri: 'http://localhost:5173/owner-app/' };
it('uses authorization code + S256 PKCE, verifies state and returns access token only', async () => {
  const store = storage();
  const url = new URL(await beginLogin(config, store));
  expect(url.searchParams.get('response_type')).toBe('code');
  expect(url.searchParams.get('code_challenge_method')).toBe('S256');
  const state = url.searchParams.get('state');
  const token = await finishLogin(config, `?code=abc&state=${state}`, store, async (_url, init) => {
    expect(init.body.get('code_verifier')).toHaveLength(86);
    return new Response(JSON.stringify({ access_token: 'access', id_token: 'id', expires_in: 3600 }));
  });
  expect(token.accessToken).toBe('access');
  expect(token).not.toHaveProperty('id_token');
  await expect(finishLogin(config, '?code=evil&state=wrong', store)).rejects.toThrow(/state/i);
});
