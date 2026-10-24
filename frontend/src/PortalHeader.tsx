import React from 'react';

export function PortalHeader({ signedIn, email, isVenue, signOut }: {
  signedIn: boolean; email?: string; isVenue: boolean; signOut: () => void;
}) {
  return <header><a className="brand" href={isVenue ? '/venue-portal/' : '/owner-app/'}>Lost<span>Link</span></a>
    <nav aria-label="Account navigation">
      {signedIn && <span className="account-email" aria-label="Signed-in email" title={email || undefined}>{email || 'Email unavailable'}</span>}
      <a href="/owner-app/">Owner App</a><a href="/venue-portal/">Venue Portal</a>
      {signedIn && <button className="secondary" onClick={signOut}>Sign out</button>}
    </nav>
  </header>;
}
