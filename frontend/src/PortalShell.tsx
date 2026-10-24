import React, { useEffect, useMemo, useState } from 'react';
import { OwnerApp, VenueApp } from './App';
import { PortalHeader } from './PortalHeader';
import { createApi } from './api.js';
import { beginLogin, finishLogin } from './auth.js';
import { type Config } from './config';

type Portal = 'owner' | 'venue';
type Session = { accessToken: string; expiresAt: number; email?: string; returnPath?: string };
const portalAtLocation = (): Portal => location.pathname.startsWith('/venue-portal') ? 'venue' : 'owner';
const pathFor = (portal: Portal) => portal === 'venue' ? '/venue-portal/' : '/owner-app/';
export const authFor = (settings: Config, portal: Portal) => ({
  domain: settings.auth.domain,
  clientId: portal === 'venue' ? settings.auth.venueClientId : settings.auth.ownerClientId,
  redirectUri: `${location.origin}${pathFor(portal)}`,
});

export function PortalShell({ settings, redirect = (url: string) => location.assign(url) }: {
  settings: Config; redirect?: (url: string) => void;
}) {
  const [portal, setPortal] = useState<Portal>(portalAtLocation);
  // Separate app clients never reuse one another's access tokens. Nothing is persisted.
  const [sessions, setSessions] = useState<Record<string, Session>>({});
  const [error, setError] = useState(''), [loading, setLoading] = useState(/[?&](code|error)=/.test(location.search));
  const config = authFor(settings, portal), session = sessions[config.clientId];
  const api = useMemo(() => createApi(settings.api, () => session && session.expiresAt > Date.now() ? session.accessToken : ''), [session, settings]);
  useEffect(() => {
    if (!/[?&](code|error)=/.test(location.search)) return;
    const callback = authFor(settings, portalAtLocation()), query = location.search;
    // Clear authorization codes from the address bar even when exchange fails.
    history.replaceState({}, '', location.pathname);
    void finishLogin(callback, query).then(value => {
      setSessions({ [callback.clientId]: value });
      history.replaceState({}, '', value.returnPath || pathFor(portalAtLocation()));
      setPortal(portalAtLocation());
    }).catch(e => {
      setError(e.message); setPortal('owner'); history.replaceState({}, '', '/owner-app/');
    }).finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    if (!session) return;
    const timer = setTimeout(() => { setSessions({}); setError('Your session expired. Sign in again.'); }, Math.max(0, session.expiresAt - Date.now()));
    return () => clearTimeout(timer);
  }, [session]);
  useEffect(() => {
    const changed = () => {
      const next = portalAtLocation(), available = sessions[authFor(settings, next).clientId];
      if (available?.expiresAt > Date.now()) setPortal(next);
      else if (session) history.replaceState({}, '', pathFor(portal));
    };
    addEventListener('popstate', changed);
    return () => removeEventListener('popstate', changed);
  }, [sessions, settings, session, portal]);
  const switchPortal = async (next: Portal) => {
    if (loading || next === portal) return;
    const target = authFor(settings, next), available = sessions[target.clientId];
    setError('');
    if (available?.expiresAt > Date.now()) {
      history.pushState({}, '', pathFor(next)); setPortal(next); return;
    }
    setLoading(true);
    try {
      // Cognito reuses its own login cookie across clients in the same user pool.
      // Default authorization also supports classic hosted UI, unlike prompt=none.
      redirect(await beginLogin({ ...target, returnPath: pathFor(next) }));
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not switch portals.'); setLoading(false); }
  };
  const signIn = async () => {
    setError(''); setLoading(true);
    try {
      const match = /^\/matches\/[^/?#]+\/?$/.test(location.pathname) ? location.pathname : '/owner-app/';
      redirect(await beginLogin({ ...authFor(settings, 'owner'), returnPath: match }));
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not sign in.'); setLoading(false); }
  };
  const signOut = () => {
    setSessions({}); sessionStorage.removeItem('lostlink.pkce');
    const url = new URL('/logout', config.domain);
    url.search = new URLSearchParams({ client_id: config.clientId, logout_uri: `${location.origin}/owner-app/` }).toString();
    redirect(url.href);
  };
  return <><PortalHeader signedIn={!!session} email={session?.email} isVenue={portal === 'venue'} signOut={signOut} navigate={next => void switchPortal(next)} />
    <main>{error && <p role="alert">{error}</p>}
      {loading ? <section><h1>{session ? 'Opening your workspace' : 'Signing you in'}</h1><p role="status">{session ? 'Switching portals securely…' : 'Completing sign-in…'}</p></section> : session ? portal === 'venue' ? <VenueApp api={api} /> : <OwnerApp api={api} /> :
        <section className="sign-in"><div><p className="eyebrow">LOSTLINK</p><h1>A way back to you.</h1><p>Find your belongings, follow your claims, and help lost items get home.</p><button onClick={() => void signIn()}>Sign in with Cognito</button><p className="sign-in-note">One account for finding belongings and managing your venue.</p></div><div className="sign-in-story" aria-hidden="true"><span>LOST</span><div className="sign-in-line" /><span>FOUND</span><p>Small details.<br />Meaningful connections.</p></div></section>}
    </main><footer>LostLink · Team 12 · Connecting people with their belongings</footer></>;
}
