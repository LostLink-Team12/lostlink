const base64url = bytes => btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
const random = length => base64url(crypto.getRandomValues(new Uint8Array(length)));
const pendingKey = 'lostlink.pkce';

export async function beginLogin(config, storage = sessionStorage) {
  if (!config.domain || !config.clientId || !config.redirectUri) throw Error('Cognito login is not configured. Set the public domain and app client ID in config.json or VITE_* settings.');
  const verifier = random(64), state = random(32);
  const challenge = base64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))));
  storage.setItem(pendingKey, JSON.stringify({ verifier, state, created: Date.now() }));
  const url = new URL('/oauth2/authorize', config.domain);
  url.search = new URLSearchParams({ response_type: 'code', client_id: config.clientId, redirect_uri: config.redirectUri, scope: 'openid email profile', state, code_challenge_method: 'S256', code_challenge: challenge }).toString();
  return url.href;
}

export async function finishLogin(config, query, storage = sessionStorage, fetcher = fetch) {
  const params = new URLSearchParams(query);
  const saved = JSON.parse(storage.getItem(pendingKey) || 'null');
  storage.removeItem(pendingKey);
  if (!saved || params.get('state') !== saved.state || Date.now() - saved.created > 600000) throw Error('Login state expired or did not match. Sign in again.');
  if (params.has('error') || !params.get('code')) throw Error('Login was not completed. Sign in again.');
  const body = new URLSearchParams({ grant_type: 'authorization_code', client_id: config.clientId, redirect_uri: config.redirectUri, code: params.get('code'), code_verifier: saved.verifier });
  const response = await fetcher(new URL('/oauth2/token', config.domain), { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body, signal: AbortSignal.timeout(30000) });
  const value = await response.json();
  if (!response.ok || !value.access_token || !Number.isFinite(value.expires_in)) throw Error('Could not complete login. Sign in again.');
  return { accessToken: value.access_token, expiresAt: Date.now() + value.expires_in * 1000 };
}
