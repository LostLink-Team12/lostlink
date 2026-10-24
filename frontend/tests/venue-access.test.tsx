import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { VenueApp } from '../src/App';

const none = { venue: null, can_manage_venue: false, can_manage_members: false, can_onboard_venues: false };

it('does not render venue operations or fetch their data while checking or denying access', async () => {
  let complete!: (value: any) => void;
  const api = vi.fn(() => new Promise(resolve => { complete = resolve; }));
  render(<VenueApp api={api} />);
  expect(screen.getByText('Checking venue access…')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Register item' })).not.toBeInTheDocument();
  complete(none);
  await screen.findByRole('heading', { name: 'Venue access required' });
  expect(screen.getByRole('link', { name: 'Go to Owner App' })).toHaveAttribute('href', '/owner-app/');
  expect(screen.queryByText('Staff management (venue administrators)')).not.toBeInTheDocument();
  expect(screen.queryByText('Venue onboarding (platform administrators)')).not.toBeInTheDocument();
  expect(api).toHaveBeenCalledExactlyOnceWith('venue', '/v1/session');
});

it.each([
  [true, false, false],
  [true, true, false],
  [false, false, true],
  [true, true, true],
])('shows only permitted capabilities: staff=%s members=%s platform=%s', async (staff, members, platform) => {
  const api = vi.fn(async (_s, path) => path === '/v1/session' ? { ...none, venue: staff ? { name: 'Campus' } : null, can_manage_venue: staff, can_manage_members: members, can_onboard_venues: platform } : { items: [] });
  render(<VenueApp api={api} />);
  await waitFor(() => expect(screen.queryByText('Checking venue access…')).not.toBeInTheDocument());
  expect(!!screen.queryByRole('button', { name: 'Register item' })).toBe(staff);
  expect(!!screen.queryByRole('link', { name: /Staff management/ })).toBe(members);
  expect(!!screen.queryByRole('link', { name: /Venue onboarding/ })).toBe(platform);
  if (members) { fireEvent.click(screen.getByRole('link', { name: /Staff management/ })); await screen.findByLabelText('Cognito subject'); }
  if (platform) { fireEvent.click(screen.getByRole('link', { name: /Venue onboarding/ })); expect(screen.getByRole('button', { name: 'Create venue' })).toBeInTheDocument(); }
  if (!staff) expect(api.mock.calls.every(([,p]) => p === '/v1/session')).toBe(true);
});

it('fails closed on access service errors and can retry successfully', async () => {
  const api = vi.fn().mockRejectedValueOnce(Error('Service unavailable')).mockResolvedValueOnce(none);
  render(<VenueApp api={api} />);
  await screen.findByRole('alert');
  expect(screen.queryByRole('button', { name: 'Register item' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry access check' }));
  await screen.findByRole('heading', { name: 'Venue access required' });
  expect(api).toHaveBeenCalledTimes(2);
});

it('removes operations when a refreshed membership is no longer active', async () => {
  let revoked = false;
  const api = vi.fn(async (_s, path) => path === '/v1/session' ? revoked ? none : { ...none, venue: { name: 'Campus' }, can_manage_venue: true } : { items: [] });
  render(<VenueApp api={api} />);
  await screen.findByRole('button', { name: 'Register item' });
  revoked = true;
  fireEvent.click(screen.getByRole('button', { name: 'Refresh records' }));
  await screen.findByRole('heading', { name: 'Venue access required' });
  expect(screen.queryByRole('button', { name: 'Register item' })).not.toBeInTheDocument();
});
