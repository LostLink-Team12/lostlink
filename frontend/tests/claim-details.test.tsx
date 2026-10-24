import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { OwnerApp, VenueApp } from '../src/App';

it.each([false, true])('replaces the clicked summary with details in place, without adding a bottom card; venue=%s', async venue => {
  const rows = [{ id: 'c1', item_title: 'Razer mouse', state: 'cancelled', version: 2 }, { id: 'c2', item_title: 'Bottle', state: 'submitted', version: 1 }];
  const api = vi.fn(async (_s, path) => {
    if (path === '/v1/session') return { venue: { name: 'Campus' }, can_manage_venue: true };
    if (path.includes('?')) return { items: rows };
    const claim = rows.find(row => path.endsWith(row.id))!;
    return { ...claim, answers: [{ question_id: 'q1', prompt: 'Model?', answer: claim.id === 'c1' ? 'Viper' : 'Bottle model' }] };
  });
  history.replaceState({}, '', venue ? '/venue-portal/#reviews' : '/owner-app/#claims');
  render(venue ? <VenueApp api={api} /> : <OwnerApp api={api} />);
  const mouse = await screen.findByRole('article', { name: 'Claim for Razer mouse' });
  const bottle = screen.getByRole('article', { name: 'Claim for Bottle' });
  fireEvent.click(within(mouse).getByRole('button', { name: `${venue ? 'Review' : 'View'} Razer mouse` }));
  expect(await within(mouse).findByText('Viper')).toBeInTheDocument();
  expect(screen.getByRole('article', { name: 'Claim for Razer mouse' })).toBe(mouse);
  expect(screen.getAllByRole('article')).toHaveLength(2);
  expect(mouse).toHaveClass('review');
  expect(within(bottle).queryByText('Viper')).not.toBeInTheDocument();
  fireEvent.click(within(bottle).getByRole('button', { name: `${venue ? 'Review' : 'View'} Bottle` }));
  await within(bottle).findByText('Bottle model');
  expect(mouse).not.toHaveClass('review');
  expect(within(mouse).queryByText('Viper')).not.toBeInTheDocument();
  fireEvent.click(within(bottle).getByRole('button', { name: 'Hide details' }));
  expect(bottle).not.toHaveClass('review');
  expect(within(bottle).queryByText('Bottle model')).not.toBeInTheDocument();
});

it('keeps detail-loading errors and retry inside the clicked card', async () => {
  history.replaceState({}, '', '/owner-app/#claims');
  render(<OwnerApp api={async (_s, path) => {
    if (path.includes('?')) return { items: [{ id: 'c1', item_title: 'Mouse', state: 'submitted', version: 1 }] };
    throw Error('Details unavailable');
  }} />);
  const card = await screen.findByRole('article', { name: 'Claim for Mouse' });
  fireEvent.click(within(card).getByRole('button', { name: 'View Mouse' }));
  expect(await within(card).findByRole('alert')).toHaveTextContent('Details unavailable');
  expect(within(card).getByRole('button', { name: 'View Mouse' })).toBeEnabled();
});
