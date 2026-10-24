import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { OwnerApp, VenueApp } from '../src/App';

it.each([
  ['owner', 'reports', 'Lost report pages', '/v1/lost-reports', 'closed'],
  ['owner', 'claims', 'My claim pages', '/v1/claims', 'cancelled'],
  ['venue', 'items', 'Found item pages', '/v1/items', 'available'],
  ['venue', 'reviews', 'Claim review pages', '/v1/venue/claims', 'submitted'],
  ['venue', 'members', 'Staff pages', '/v1/venues/current/members', 'active'],
])('paginates %s/%s using its own server result and page controls', async (portal, route, label, path, state) => {
  const rows = Array.from({ length: 25 }, (_, i) => ({ id: `id-${i}`, title: `Record ${i}`, description: `Record ${i}`, item_title: `Record ${i}`, user_sub: `Record ${i}`, category: 'Mug', state, role: 'venue_staff', status: 'active', version: 1 }));
  const api = vi.fn(async (_s, url) => {
    if (url === '/v1/session') return { venue: { name: 'Campus' }, can_manage_venue: true, can_manage_members: true };
    if (!url.startsWith(path + '?')) return { items: [], has_more: false };
    const offset = Number(new URL(url, 'https://test.invalid').searchParams.get('offset'));
    return { items: rows.slice(offset, offset + 20), has_more: offset === 0 };
  });
  history.replaceState({}, '', `/${portal === 'venue' ? 'venue-portal' : 'owner-app'}/#${route}`);
  render(portal === 'venue' ? <VenueApp api={api} /> : <OwnerApp api={api} />);
  await screen.findByRole('heading', { name: 'Record 0', exact: true });
  const pages = screen.getByRole('navigation', { name: label });
  expect(within(pages).getByRole('button', { name: 'Previous' })).toBeDisabled();
  expect(within(pages).getByRole('button', { name: 'Next' })).toBeEnabled();
  fireEvent.click(within(pages).getByRole('button', { name: 'Next' }));
  await screen.findByRole('heading', { name: 'Record 20', exact: true });
  expect(api).toHaveBeenCalledWith(portal === 'venue' && route === 'members' ? 'venue' : route === 'items' ? 'item' : route === 'reports' ? 'matching' : 'claim', `${path}?limit=20&offset=20`);
  expect(within(pages).getByText('Showing 21–25')).toBeInTheDocument();
  expect(within(pages).getByRole('button', { name: 'Next' })).toBeDisabled();
  expect(within(pages).getByRole('button', { name: 'Previous' })).toBeEnabled();
  fireEvent.click(within(pages).getByRole('button', { name: 'Previous' }));
  await screen.findByRole('heading', { name: 'Record 0', exact: true });
  expect(within(pages).getByRole('button', { name: 'Previous' })).toBeDisabled();
});
