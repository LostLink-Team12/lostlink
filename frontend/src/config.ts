type Env = Record<string, string | undefined>;
const services = ['venue', 'item', 'matching', 'claim'] as const;
export type Config = {
  api: Record<typeof services[number], string>;
  auth: { domain: string; ownerClientId: string; venueClientId: string };
};

// Public deployment settings only. Runtime values take precedence over VITE_*.
export async function loadConfig(env: Env, fetcher = fetch): Promise<Config> {
  const response = await fetcher('/config.json', { cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(10000) });
  let runtime: any = {};
  if (response.status !== 404) {
    if (!response.ok) throw Error(`Could not load config.json (${response.status})`);
    try { runtime = await response.json(); } catch { throw Error('config.json must contain valid JSON.'); }
    if (!runtime || typeof runtime !== 'object' || Array.isArray(runtime)) throw Error('config.json must contain an object.');
  }
  const value = (candidate: unknown, fallback: string | undefined) => {
    if (candidate !== undefined && typeof candidate !== 'string') throw Error('config.json values must be strings.');
    return (typeof candidate === 'string' && candidate.trim()) || fallback?.trim() || '';
  };
  const api = Object.fromEntries(services.map(service => {
    const base = value(runtime.api?.[service], env[`VITE_${service.toUpperCase()}_API_URL`]);
    if (base) {
      const localPath = base.startsWith('/') && !base.startsWith('//');
      const parsed = localPath ? null : new URL(base);
      if (parsed && (parsed.username || parsed.password || parsed.search || parsed.hash || (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname))))) throw Error(`${service} API requires HTTPS or a local development URL.`);
    }
    return [service, base.replace(/\/$/, '')];
  })) as Config['api'];
  const auth = {
    domain: value(runtime.auth?.domain, env.VITE_COGNITO_DOMAIN),
    ownerClientId: value(runtime.auth?.ownerClientId, env.VITE_OWNER_CLIENT_ID),
    venueClientId: value(runtime.auth?.venueClientId, env.VITE_VENUE_CLIENT_ID),
  };
  if (auth.domain) {
    const url = new URL(auth.domain);
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) throw Error('Cognito domain requires HTTPS.');
  }
  return { api, auth };
}

// Development only: same-origin browser requests avoid depending on backend CORS.
// Production still needs gateway CORS or the equivalent same-origin routing.
export function devProxy(env: Env) {
  return Object.fromEntries(services.flatMap(service => {
    const target = env[`DEV_${service.toUpperCase()}_API_TARGET`];
    if (!target) return [];
    const prefix = `/api/${service}`;
    return [[prefix, { target, changeOrigin: true, rewrite: (path: string) => path.slice(prefix.length) }]];
  }));
}
