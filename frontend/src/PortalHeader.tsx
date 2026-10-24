import React from 'react';

export function PortalHeader({ signedIn, email, isVenue, signOut, navigate }: {
  signedIn: boolean; email?: string; isVenue: boolean; signOut: () => void; navigate?: (portal: 'owner' | 'venue') => void;
}) {
  return <header><a className="brand" href="/owner-app/" onClick={e => { if (signedIn && navigate) { e.preventDefault(); navigate('owner'); } }}>Lost<span>Link</span></a>
    <nav aria-label="Account navigation">
      {signedIn && <span className="account-email" aria-label="Signed-in email" title={email || undefined}>{email || 'Email unavailable'}</span>}
      {signedIn && <><a href="/owner-app/" onClick={e => { if (navigate) { e.preventDefault(); navigate('owner'); } }}>Owner App</a><a href="/venue-portal/" onClick={e => { if (navigate) { e.preventDefault(); navigate('venue'); } }}>Venue Portal</a></>}
      {signedIn && <button className="secondary" onClick={signOut}>Sign out</button>}
    </nav>
  </header>;
}
