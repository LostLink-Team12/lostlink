import React, { useState } from 'react';

export const dateRangeMessage = 'Found from must be earlier than or equal to Found until.';

export function foundDateError(from: string, until: string) {
  if (from && !Number.isFinite(new Date(from).getTime())) return 'Found from must be a valid date and time.';
  if (until && !Number.isFinite(new Date(until).getTime())) return 'Found until must be a valid date and time.';
  return from && until && new Date(from) > new Date(until) ? dateRangeMessage : '';
}

export function foundDateRange(from: string, until: string) {
  const error = foundDateError(from, until);
  if (error) throw Error(error);
  return { ...(from ? { found_from: new Date(from).toISOString() } : {}), ...(until ? { found_until: new Date(until).toISOString() } : {}) };
}

export function FoundDateFilters() {
  const [from, setFrom] = useState(''), [until, setUntil] = useState('');
  const error = foundDateError(from, until);
  return <><label>Found from<input name="from" type="datetime-local" value={from} onChange={e => setFrom(e.target.value)} aria-invalid={!!error} aria-describedby={error ? 'found-date-error' : undefined} /></label>
    <label>Found until<input name="until" type="datetime-local" value={until} onChange={e => setUntil(e.target.value)} aria-invalid={!!error} aria-describedby={error ? 'found-date-error' : undefined} /></label>
    {error && <p id="found-date-error" role="alert">{error}</p>}</>;
}
