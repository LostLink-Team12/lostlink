import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { OwnerApp, VenueApp } from './App';
import { createApi } from './api.js';
import { beginLogin, finishLogin } from './auth.js';
import { loadConfig, type Config } from './config';
import './style.css';
import { PortalHeader } from './PortalHeader';

const env = import.meta.env;
const isVenue = location.pathname.startsWith('/venue-portal');
function App({ settings }: { settings: Config }) {
  const config = { domain: settings.auth.domain, clientId: isVenue ? settings.auth.venueClientId : settings.auth.ownerClientId, redirectUri: `${location.origin}/${isVenue ? 'venue-portal' : 'owner-app'}/` };
  const [session, setSession] = useState<{ accessToken: string; expiresAt: number; email?: string } | null>(null), [error, setError] = useState(''), [loading, setLoading] = useState(false);
  const api = useMemo(() => createApi(settings.api, () => session && session.expiresAt > Date.now() ? session.accessToken : ''), [session, settings]);
  useEffect(() => {
    if (!/[?&](code|error)=/.test(location.search)) return;
    const query = location.search; const target = sessionStorage.getItem('lostlink.returnPath'); sessionStorage.removeItem('lostlink.returnPath'); history.replaceState({}, '', target && /^\/matches\/[^/?#]+\/?$/.test(target) ? target : location.pathname); setLoading(true);
    finishLogin(config, query).then(setSession).catch(e => setError(e.message)).finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    if (!session) return;
    const timer = setTimeout(() => { setSession(null); setError('Your session expired. Sign in again.'); }, Math.max(0, session.expiresAt - Date.now()));
    return () => clearTimeout(timer);
  }, [session]);
  return <><PortalHeader signedIn={!!session} email={session?.email} isVenue={isVenue} signOut={() => {
    setSession(null); sessionStorage.removeItem('lostlink.pkce');
    const url = new URL('/logout', config.domain); url.search = new URLSearchParams({ client_id: config.clientId, logout_uri: config.redirectUri }).toString(); location.assign(url.href);
  }} /><main>{error && <p role="alert">{error}</p>}{session ? isVenue ? <VenueApp api={api} /> : <OwnerApp api={api} /> : <section><p className="eyebrow">CROSS-VENUE LOST & FOUND</p><h1>A way back to you.</h1><p>{isVenue ? 'Sign in to manage your venue’s found items and claims.' : 'Sign in to find an item, report a loss or check your claims.'}</p><button disabled={loading} onClick={() => { if (/^\/matches\/[^/?#]+\/?$/.test(location.pathname)) sessionStorage.setItem('lostlink.returnPath', location.pathname); void beginLogin(config).then(url => location.assign(url)).catch(e => setError(e.message)); }}>{loading ? 'Signing in…' : 'Sign in with Cognito'}</button></section>}</main><footer>LostLink · Team 12 · Connecting people with their belongings</footer></>;
}
const root = createRoot(document.getElementById('root')!);
root.render(<p role="status">Loading settings…</p>);
void loadConfig(env).then(settings => root.render(<App settings={settings} />)).catch(error => {
  root.render(<main><h1>LostLink</h1><p role="alert">{error.message}</p><button onClick={() => location.reload()}>Retry loading settings</button></main>);
});
