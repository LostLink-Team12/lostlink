import React, { useEffect, useState } from 'react';
import type { Api } from './App';

type Contact = { name?: string | null; phone?: string | null; email: string };

export function ClaimContact({ api, claimId, venue, state, version }: { api: Api; claimId: string; venue: boolean; state: string; version: number }) {
  const [contact, setContact] = useState<Contact | null>(null), [error, setError] = useState(''), [attempt, setAttempt] = useState(0), [loading, setLoading] = useState(true);
  const allowed = venue ? ['submitted', 'approval_pending', 'approved', 'handover_pending'].includes(state) : ['approved', 'handover_pending', 'handed_over'].includes(state);
  useEffect(() => {
    let current = true; setContact(null); setError(''); setLoading(true);
    if (!allowed) { setLoading(false); return; }
    const path = venue ? `/v1/venue/claims/${encodeURIComponent(claimId)}/contact` : `/v1/claims/${encodeURIComponent(claimId)}/venue-contact`;
    api('claim', path).then(value => { if (current) setContact(value); }).catch(e => {
      if (current) setError(!venue && e.code === 'contact_not_available' ? 'The venue has not shared contact details yet. Please check again later.' : 'Contact details could not be loaded. Please retry.');
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [api, claimId, venue, allowed, state, version, attempt]);
  if (!allowed) return null;
  return <div className="claim-contact" role="group" aria-label={venue ? 'Claimant contact' : 'Venue contact'}>
    <h4>{venue ? 'Claimant contact' : 'Venue contact'}</h4>
    {loading ? <p role="status">Loading contact details…</p> : error ? <><p role="alert">{error}</p><button type="button" className="secondary" onClick={() => setAttempt(value => value + 1)}>Retry contact details</button></> : contact && <dl>
      <dt>Name</dt><dd>{contact.name || 'Not provided'}</dd>
      <dt>Phone</dt><dd>{contact.phone || 'Not provided'}</dd>
      <dt>Email</dt><dd>{contact.email || 'Not provided'}</dd>
    </dl>}
  </div>;
}

export function VenueContactFields() {
  return <fieldset><legend>Venue contact shared with the owner after approval</legend>
    <p>Your verified sign-in email will be shared as the reply email.</p>
    <label>Venue contact name<input name="venue_name" required maxLength={120} /></label>
    <label>Venue contact phone (optional)<input name="venue_phone" type="tel" minLength={3} maxLength={32} /></label>
  </fieldset>;
}
