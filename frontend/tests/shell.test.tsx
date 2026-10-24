import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { it, expect, vi, beforeEach } from 'vitest';
import { PortalShell } from '../src/PortalShell';
import { beginLogin, finishLogin } from '../src/auth.js';
import { createApi } from '../src/api.js';

vi.mock('../src/auth.js', () => ({ beginLogin: vi.fn(), finishLogin: vi.fn() }));
vi.mock('../src/api.js', async original => ({ ...await original<any>(), createApi: vi.fn(() => vi.fn(async () => ({ venue: null, can_manage_venue: false, can_manage_members: false, can_onboard_venues: false, items: [] }))) }));
const settings = { auth: { domain: 'https://example.com', ownerClientId: 'owner', venueClientId: 'venue' }, api: { venue: '/venue', item: '/item', claim: '/claim', matching: '/matching' } };
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(beginLogin).mockResolvedValue('https://example.com/oauth2/authorize');
  vi.mocked(finishLogin).mockResolvedValue({ accessToken: 'owner-access', email: 'owner@example.com', expiresAt: Date.now() + 3600000 });
});

it('offers one sign-in screen without portal selectors and signs into Owner even from Venue', async () => {
  history.replaceState({}, '', '/venue-portal/');
  const redirect = vi.fn(); render(<PortalShell settings={settings} redirect={redirect} />);
  expect(screen.queryByRole('link', { name: 'Owner App' })).not.toBeInTheDocument();
  expect(screen.queryByRole('link', { name: 'Venue Portal' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Sign in with Cognito' }));
  await waitFor(() => expect(redirect).toHaveBeenCalled());
  expect(beginLogin).toHaveBeenCalledWith(expect.objectContaining({ clientId: 'owner', redirectUri: `${location.origin}/owner-app/`, returnPath: '/owner-app/' }));
});

it('switches shared-client portals in memory without another login and switches back', async () => {
  history.replaceState({}, '', '/owner-app/?code=abc&state=test');
  const redirect = vi.fn();
  render(<PortalShell settings={{ ...settings, auth: { ...settings.auth, venueClientId: 'owner' } }} redirect={redirect} />);
  await screen.findByRole('heading', { name: 'Find what matters.' });
  fireEvent.click(screen.getByRole('link', { name: 'Venue Portal' }));
  await screen.findByRole('heading', { name: 'Venue access required' });
  expect(location.pathname).toBe('/venue-portal/');
  expect(redirect).not.toHaveBeenCalled();
  expect(beginLogin).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('link', { name: 'Owner App' }));
  await screen.findByRole('heading', { name: 'Find what matters.' });
  expect(location.pathname).toBe('/owner-app/');
});

it('automatically authorizes the destination client instead of returning to the sign-in screen', async () => {
  history.replaceState({}, '', '/owner-app/?code=abc&state=test');
  const redirect = vi.fn(); render(<PortalShell settings={settings} redirect={redirect} />);
  await screen.findByRole('heading', { name: 'Find what matters.' });
  fireEvent.click(screen.getByRole('link', { name: 'Venue Portal' }));
  await waitFor(() => expect(redirect).toHaveBeenCalledWith('https://example.com/oauth2/authorize'));
  expect(beginLogin).toHaveBeenCalledWith(expect.objectContaining({ clientId: 'venue', redirectUri: `${location.origin}/venue-portal/`, returnPath: '/venue-portal/' }));
  expect(screen.queryByRole('button', { name: 'Sign in with Cognito' })).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('Switching portals securely');
});

it('uses the destination callback session and handles an expired SSO session without redirect loops', async () => {
  history.replaceState({}, '', '/venue-portal/?error=login_required&state=test');
  vi.mocked(finishLogin).mockRejectedValueOnce(Error('Your sign-in session ended. Sign in again.'));
  const redirect = vi.fn(); render(<PortalShell settings={settings} redirect={redirect} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Your sign-in session ended');
  expect(location.pathname).toBe('/owner-app/');
  expect(screen.getByRole('button', { name: 'Sign in with Cognito' })).toBeEnabled();
  expect(redirect).not.toHaveBeenCalled();
});

it('completes a venue SSO callback with that client token and displays its email', async () => {
  history.replaceState({}, '', '/venue-portal/?code=abc&state=test');
  vi.mocked(finishLogin).mockResolvedValueOnce({ accessToken: 'venue-access', email: 'owner@example.com', expiresAt: Date.now() + 3600000, returnPath: '/venue-portal/' });
  render(<PortalShell settings={settings} redirect={vi.fn()} />);
  await screen.findByRole('heading', { name: 'Venue access required' });
  expect(finishLogin).toHaveBeenCalledWith(expect.objectContaining({ clientId: 'venue' }), '?code=abc&state=test');
  expect(vi.mocked(createApi).mock.calls.at(-1)![1]()).toBe('venue-access');
  expect(screen.getByLabelText('Signed-in email')).toHaveTextContent('owner@example.com');
});
