import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { OwnerApp } from '../src/App';

async function openClaim(api: any) {
  history.replaceState({}, '', '/owner-app/#claims');
  render(<OwnerApp api={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'View Blue bag' }));
  await screen.findByRole('button', { name: 'Refresh claim details' });
  expect(screen.queryByRole('button', { name: 'Submit claim' })).not.toBeInTheDocument();
}

it.each([false, true])('withdraws with confirmation, a reason and the current version; asynchronous=%s', async asynchronous => {
  let cancelled = false;
  const api = vi.fn(async (_s, path, options) => {
    const claim = { id: 'c1', item_title: 'Blue bag', state: cancelled ? 'cancelled' : asynchronous ? 'approved' : 'submitted', version: 4 };
    if (path.includes('/cancel')) { cancelled = true; return asynchronous ? { operation_id: 'op1', version: 5 } : { ...claim, state: 'cancelled' }; }
    if (path === '/v1/claim-operations/op1') return { state: 'completed', claim_state: 'cancelled' };
    return path.includes('?') ? { items: [claim] } : claim;
  });
  await openClaim(api);
  fireEvent.click(screen.getByRole('button', { name: 'Withdraw claim' }));
  expect(api.mock.calls.some(([, p]) => p.endsWith('/cancel'))).toBe(false);
  fireEvent.change(screen.getByLabelText('Withdrawal reason'), { target: { value: 'found_elsewhere' } });
  fireEvent.click(screen.getByRole('button', { name: 'Confirm withdrawal' }));
  await screen.findByText('Claim withdrawn. This request is no longer active.');
  expect(api).toHaveBeenCalledWith('claim', '/v1/claims/c1/cancel', { body: { reason_code: 'found_elsewhere' }, version: 4 });
  expect(screen.queryByRole('button', { name: 'Withdraw claim' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Get handover code' })).not.toBeInTheDocument();
});

it('keeps an accepted withdrawal resumable after polling fails without reporting success', async () => {
  const api = vi.fn(async (_s, path) => {
    if (path.endsWith('/cancel')) return { operation_id: 'op1', version: 5 };
    if (path.includes('claim-operations')) throw Error('Connection interrupted');
    const claim = { id: 'c1', item_title: 'Blue bag', state: 'approved', version: 4 };
    return path.includes('?') ? { items: [claim] } : claim;
  });
  await openClaim(api);
  fireEvent.click(screen.getByRole('button', { name: 'Withdraw claim' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm withdrawal' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Connection interrupted');
  expect(screen.getByRole('button', { name: 'Check pending operation' })).toBeEnabled();
  expect(screen.queryByText(/Claim withdrawn/)).not.toBeInTheDocument();
});

it.each(['cancelled', 'rejected', 'handed_over', 'handover_pending', 'cancellation_pending'])('does not offer withdrawal for %s', async state => {
  await openClaim(async (_s: string, path: string) => path.includes('?') ? { items: [{ id: 'c1', item_title: 'Blue bag', state, version: 4 }] } : { id: 'c1', item_title: 'Blue bag', state, version: 4 });
  expect(screen.queryByRole('button', { name: 'Withdraw claim' })).not.toBeInTheDocument();
});
