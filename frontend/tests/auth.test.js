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
    if (String(_url).endsWith('/oauth2/userInfo')) {
      expect(init.headers.Authorization).toBe('Bearer access');
      return new Response(JSON.stringify({ sub: 'user-1', email: 'owner@example.com' }));
    }
    expect(init.body.get('code_verifier')).toHaveLength(86);
    return new Response(JSON.stringify({ access_token: 'access', id_token: 'id', expires_in: 3600 }));
  });
  expect(token.accessToken).toBe('access');
  expect(token.email).toBe('owner@example.com');
  expect(token).not.toHaveProperty('id_token');
  await expect(finishLogin(config, '?code=evil&state=wrong', store)).rejects.toThrow(/state/i);
});

it('keeps a valid login usable if profile lookup is unavailable', async () => {
  const store = storage(), url = new URL(await beginLogin(config, store));
  const session = await finishLogin(config, `?code=abc&state=${url.searchParams.get('state')}`, store, async url => {
    if (String(url).endsWith('/oauth2/userInfo')) throw Error('Profile unavailable');
    return new Response(JSON.stringify({ access_token: 'access', expires_in: 3600 }));
  });
  expect(session.accessToken).toBe('access');
  expect(session.email).toBe('');
});

it('binds the PKCE exchange to its initiating client and callback', async () => {
  const store = storage(), url = new URL(await beginLogin(config, store)), fetcher = () => { throw Error('Must not exchange'); };
  await expect(finishLogin({ ...config, clientId: 'another-client' }, `?code=abc&state=${url.searchParams.get('state')}`, store, fetcher)).rejects.toThrow(/state/i);
});

it.each(['/venue-portal/', '/matches/match-1', 'https://evil.example/', '//evil.example/'])('restricts the post-login destination %s', async returnPath => {
  const store = storage(), url = new URL(await beginLogin({ ...config, returnPath }, store));
  const session = await finishLogin(config, `?code=abc&state=${url.searchParams.get('state')}`, store, async () => new Response(JSON.stringify({ access_token: 'access', expires_in: 3600 })));
  expect(session.returnPath).toBe(returnPath.startsWith('/venue-portal') || returnPath.startsWith('/matches/') ? returnPath : undefined);
});
