import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { ClaimContact } from '../src/ClaimContact';
import { VenueApp } from '../src/App';

it.each([true, false])('shows the correct counterpart contact in claim details; venue=%s', async venue => {
  const api = vi.fn(async () => ({ name: 'Counterpart', phone: '+65 6123 4567', email: 'contact@example.com' }));
  render(<ClaimContact api={api} claimId="c1" venue={venue} state={venue ? 'submitted' : 'approved'} version={1} />);
  const group = screen.getByRole('group', { name: venue ? 'Claimant contact' : 'Venue contact' });
  expect(await within(group).findByText('contact@example.com')).toBeVisible();
  expect(within(group).getByText('+65 6123 4567')).toBeVisible();
  expect(api).toHaveBeenCalledWith('claim', venue ? '/v1/venue/claims/c1/contact' : '/v1/claims/c1/venue-contact');
});

it('does not fetch venue contacts before approval or retain them after cancellation', async () => {
  const api = vi.fn(async () => ({ email: 'venue@example.com' }));
  const { rerender } = render(<ClaimContact api={api} claimId="c1" venue={false} state="submitted" version={1} />);
  expect(api).not.toHaveBeenCalled();
  rerender(<ClaimContact api={api} claimId="c1" venue={false} state="approved" version={2} />);
  await screen.findByText('venue@example.com');
  rerender(<ClaimContact api={api} claimId="c1" venue={false} state="cancelled" version={3} />);
  expect(screen.queryByText('venue@example.com')).not.toBeInTheDocument();
});

it('shows missing optional contact fields and retries a contact failure independently', async () => {
  const api = vi.fn().mockRejectedValueOnce(Error('Offline')).mockResolvedValue({ email: 'venue@example.com' });
  render(<ClaimContact api={api} claimId="c1" venue={false} state="approved" version={1} />);
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'Retry contact details' }));
  await screen.findByText('venue@example.com');
  expect(screen.getAllByText('Not provided')).toHaveLength(2);
});

it('can backfill venue contact on an already approved claim with the current version', async () => {
  const claim = { id: 'c1', item_title: 'Wallet', state: 'approved', version: 4 };
  const api = vi.fn(async (_s, path) => {
    if (path === '/v1/session') return { venue: { name: 'Campus' }, can_manage_venue: true };
    if (path.endsWith('/contact')) return { name: 'Alex', email: 'owner@example.com' };
    if (path.endsWith('/reply-contact')) return { ...claim, version: 5 };
    return path.includes('?') ? { items: [claim] } : claim;
  });
  history.replaceState({}, '', '/venue-portal/#reviews');
  render(<VenueApp api={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review Wallet' }));
  const form = await screen.findByRole('form', { name: 'Update venue contact' });
  fireEvent.change(within(form).getByLabelText('Venue contact name'), { target: { value: 'Campus desk' } });
  fireEvent.change(within(form).getByLabelText('Venue contact phone (optional)'), { target: { value: '+65 6123 4567' } });
  fireEvent.submit(form);
  await waitFor(() => expect(api).toHaveBeenCalledWith('claim', '/v1/venue/claims/c1/reply-contact', { method: 'PUT', version: 4, body: { name: 'Campus desk', phone: '+65 6123 4567' } }));
  await screen.findByText('Venue contact shared with the owner.');
});
