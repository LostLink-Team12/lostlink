import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { OwnerApp } from '../src/App';

it('disables matches for an already closed report without loading its results', async () => {
  history.replaceState({}, '', '/owner-app/#reports');
  const api = vi.fn(async (_s, path) => path.startsWith('/v1/lost-reports?') ? { items: [{ id: 'r1', description: 'Bottle', state: 'closed', version: 2 }] } : { items: [] });
  render(<OwnerApp api={api} />);
  const button = await screen.findByRole('button', { name: 'View matches' });
  expect(button).toBeDisabled(); fireEvent.click(button);
  expect(screen.queryByRole('region', { name: 'Report matches' })).not.toBeInTheDocument();
  expect(api.mock.calls.some(([,p]) => p.includes('/r1/candidates'))).toBe(false);
  expect(screen.getByText('Matching has stopped for this closed report.')).toBeInTheDocument();
});

it('closes the selected result pane and disables the action immediately after a successful close', async () => {
  let reads = 0;
  history.replaceState({}, '', '/owner-app/#reports');
  const api = vi.fn(async (_s, path) => {
    if (path.startsWith('/v1/lost-reports?')) { if (reads++) return new Promise(() => {}); return { items: [{ id: 'r1', description: 'Bottle', state: 'open', version: 1 }] }; }
    return { items: [] };
  });
  render(<OwnerApp api={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'View matches' }));
  await screen.findByRole('region', { name: 'Report matches' });
  fireEvent.click(screen.getByRole('button', { name: 'Close lost report' }));
  await screen.findByText('Lost report closed.');
  expect(screen.getByRole('button', { name: 'View matches' })).toBeDisabled();
  expect(screen.queryByRole('region', { name: 'Report matches' })).not.toBeInTheDocument();
});
