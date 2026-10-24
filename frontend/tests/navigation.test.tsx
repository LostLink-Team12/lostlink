import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { OwnerApp, VenueApp } from '../src/App';

it('separates owner pages, loads records on demand and retains the search draft', async () => {
  const api = vi.fn(async () => ({ items: [] }));
  render(<OwnerApp api={api} />);
  fireEvent.change(screen.getByLabelText('Describe your lost item'), { target: { value: 'Blue mug' } });
  expect(api).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('link', { name: /My lost reports/ }));
  await screen.findByText('No records yet.');
  expect(screen.getByRole('heading', { name: 'My lost reports' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Search items' })).not.toBeInTheDocument();
  expect(api).toHaveBeenCalledWith('matching', '/v1/lost-reports?limit=20&offset=0');
  fireEvent.click(screen.getByRole('link', { name: /My claims/ }));
  await screen.findByRole('heading', { name: 'My claims' });
  expect(screen.queryByRole('heading', { name: 'My lost reports' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('link', { name: /Search/ }));
  expect(screen.getByLabelText('Describe your lost item')).toHaveValue('Blue mug');
  expect(screen.getByRole('link', { name: /Search/ })).toHaveAttribute('aria-current', 'page');
});

it('separates venue operations and retains an unfinished registration draft when browsing inventory', async () => {
  const api = vi.fn(async (_s, path) => path === '/v1/session' ? { venue: { name: 'Campus' }, can_manage_venue: true, can_manage_members: true, can_onboard_venues: true } : { items: [] });
  render(<VenueApp api={api} />);
  await screen.findByRole('button', { name: 'Register item' });
  fireEvent.change(screen.getByLabelText('Item title'), { target: { value: 'Test blue mug' } });
  expect(api.mock.calls.every(([,p]) => p === '/v1/session')).toBe(true);
  fireEvent.click(screen.getByRole('link', { name: /Found items/ }));
  await screen.findByText('No records yet.');
  expect(screen.queryByRole('button', { name: 'Register item' })).not.toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Claims to review' })).not.toBeInTheDocument();
  expect(api).toHaveBeenCalledWith('item', '/v1/items?limit=20&offset=0');
  fireEvent.click(screen.getByRole('link', { name: /Register item/ }));
  expect(screen.getByLabelText('Item title')).toHaveValue('Test blue mug');
  fireEvent.click(screen.getByRole('link', { name: /Venue onboarding/ }));
  expect(screen.getByRole('button', { name: 'Create venue' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Add member' })).not.toBeInTheDocument();
});
