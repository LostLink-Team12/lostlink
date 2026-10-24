import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { it, expect } from 'vitest';
import { OwnerApp, VenueApp } from '../src/App';
import { RecordTimes } from '../src/RecordTimes';
const created = '2026-10-24T08:30:00Z', approved = '2026-10-24T09:00:00Z', closed = '2026-10-24T10:00:00Z';

it.each([false, true])('shows claim lifecycle timestamps in the original card before and after expansion; venue=%s', async venue => {
  const row = { id: 'c1', item_title: 'Mouse', state: 'cancelled', version: 3, created_at: created, approved_at: approved, closed_at: closed };
  history.replaceState({}, '', venue ? '/venue-portal/#reviews' : '/owner-app/#claims');
  const { container } = render(venue ? <VenueApp api={api} /> : <OwnerApp api={api} />);
  async function api(_s: string, path: string) { if (path === '/v1/session') return { can_manage_venue: true, venue: { name: 'Campus' } }; return path.includes('?') ? { items: [row] } : row; }
  await screen.findByText('Submitted', { exact: true });
  expect(container.querySelector('time')).toHaveAttribute('datetime', '2026-10-24T08:30:00.000Z');
  expect(screen.getByText('Approved', { exact: true })).toBeInTheDocument();
  expect(screen.getByText('Closed', { exact: true })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: `${venue ? 'Review' : 'View'} Mouse` }));
  await screen.findByRole('button', { name: 'Hide details' });
  expect(container.querySelectorAll('time')).toHaveLength(3);
});

it('shows report creation time and venue item found/registration times', async () => {
  const api = async (_s: string, path: string) => {
    if (path === '/v1/session') return { can_manage_venue: true, venue: { name: 'Campus' } };
    if (path.startsWith('/v1/lost-reports')) return { items: [{ id: 'r1', description: 'Mouse report', state: 'open', created_at: created }] };
    if (path.startsWith('/v1/items')) return { items: [{ id: 'i1', title: 'Mouse', description: 'Black mouse', category: 'Mouse', state: 'available', found_at: approved, created_at: created }] };
    return { items: [] };
  };
  history.replaceState({}, '', '/owner-app/#reports');
  const owner = render(<OwnerApp api={api} />);
  await screen.findByText('Reported');
  expect(owner.container.querySelector('time')).toHaveAttribute('datetime', '2026-10-24T08:30:00.000Z');
  owner.unmount(); history.replaceState({}, '', '/venue-portal/#items');
  const venue = render(<VenueApp api={api} />);
  await screen.findByText('Registered');
  expect(screen.getByText('Found', { exact: true })).toBeInTheDocument();
  expect(venue.container.querySelectorAll('time')).toHaveLength(2);
});

it('does not invent timestamps for missing or malformed server fields', () => {
  const { container } = render(<RecordTimes entries={[{ label: 'Reported' }, { label: 'Approved', value: 'invalid' }]} />);
  expect(container.querySelector('time')).toBeNull();
});
