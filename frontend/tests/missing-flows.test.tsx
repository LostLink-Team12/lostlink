import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { it, expect, vi, afterEach } from 'vitest';
import { OwnerApp, VenueApp } from '../src/App';
import { MatchLink, Members, ScanToken } from '../src/MissingFlows';

afterEach(() => history.replaceState({}, '', '/'));

function venueApi(state = 'submitted') {
  return vi.fn(async (_service: string, path: string, options?: any): Promise<any> => {
    if (path === '/v1/session') return { venue: { name: 'Campus' }, can_manage_venue: true, can_manage_members: true, can_onboard_venues: false };
    if (path.startsWith('/v1/venue/claims?')) return { items: [{ id: 'c1', item_title: 'Bag', state }] };
    if (path === '/v1/venue/claims/c1') return { id: 'c1', item_title: 'Bag', state, version: 7 };
    if (path.endsWith('/reject')) return { id: 'c1', item_title: 'Bag', state: 'rejected', version: 8 };
    if (path.endsWith('/handover') && options) return { operation_id: 'op1' };
    if (path === '/v1/claim-operations/op1') return { state: 'completed', claim_state: 'handed_over' };
    return { items: [] };
  });
}

it.each(['answers_incorrect', 'insufficient_detail', 'duplicate', 'other'])('rejects using reason %s and the detail version', async reason => {
  const api = venueApi(); render(<VenueApp api={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review Bag' }));
  fireEvent.change(await screen.findByLabelText('Rejection reason'), { target: { value: reason } });
  fireEvent.click(screen.getByRole('button', { name: 'Reject claim' }));
  await screen.findByText('Claim rejected.');
  expect(api).toHaveBeenCalledWith('claim', '/v1/venue/claims/c1/reject', { body: { reason_code: reason }, version: 7 });
});

it('submits the unmodified QR payload and polls the accepted handover operation', async () => {
  const api = venueApi('approved'); render(<VenueApp api={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review Bag' }));
  const payload = '  lostlink:original-QR_payload+abc/==  ';
  fireEvent.change(await screen.findByLabelText('Handover payload'), { target: { value: payload } });
  fireEvent.click(screen.getByRole('button', { name: 'Complete handover' }));
  await screen.findByText('Item handed over.');
  expect(api).toHaveBeenCalledWith('claim', '/v1/venue/claims/c1/handover', { body: { token: payload }, version: 7 });
  expect(api).toHaveBeenCalledWith('claim', '/v1/claim-operations/op1');
});

it('keeps an accepted handover operation resumable when polling fails', async () => {
  const base = venueApi('approved');
  const api = vi.fn(async (s, p, o) => { if (p === '/v1/claim-operations/op1') throw Error('Polling unavailable'); return base(s,p,o); });
  render(<VenueApp api={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review Bag' }));
  fireEvent.change(await screen.findByLabelText('Handover payload'), { target: { value: 'original-payload-long-enough' } });
  fireEvent.click(screen.getByRole('button', { name: 'Complete handover' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Polling unavailable');
  expect(screen.getByRole('button', { name: 'Check pending operation' })).toBeEnabled();
  expect(screen.queryByText('Item handed over.')).not.toBeInTheDocument();
});

it('offers manual input when browser QR scanning is unavailable', async () => {
  render(<ScanToken busy={false} submit={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Scan QR code' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Paste the original handover payload');
  expect(screen.getByLabelText('Handover payload')).toBeEnabled();
});

it('closes a report with its version and selected reason', async () => {
  let closed = false;
  const api = vi.fn(async (_s, path, options) => {
    if (path.startsWith('/v1/lost-reports?')) return { items: [{ id: 'r1', description: 'Umbrella', state: closed ? 'closed' : 'open', version: 3 }] };
    if (options && path.endsWith('/close')) { closed = true; return {}; }
    return { items: [] };
  });
  render(<OwnerApp api={api} />);
  fireEvent.change(await screen.findByLabelText('Closure reason'), { target: { value: 'no_longer_needed' } });
  fireEvent.click(screen.getByRole('button', { name: 'Close lost report' }));
  await screen.findByText('Lost report closed.');
  expect(api).toHaveBeenCalledWith('matching', '/v1/lost-reports/r1/close', { body: { reason: 'no_longer_needed' }, version: 3 });
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Close lost report' })).not.toBeInTheDocument());
});

it('shows version conflicts without claiming success', async () => {
  const base = venueApi();
  const api = vi.fn(async (s,p,o) => { if (p.endsWith('/reject')) throw Error('Version conflict. Refresh records.'); return base(s,p,o); });
  render(<VenueApp api={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review Bag' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Reject claim' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Version conflict');
  expect(screen.queryByText('Claim rejected.')).not.toBeInTheDocument();
});

it('adds staff and patches roles and suspension with If-Match', async () => {
  const api = vi.fn(async (_s,p,o) => o ? { identity_sync_operation_id: 'sync1' } : { items: [{ id: 'm1', user_sub: 'staff-sub', role: 'venue_staff', status: 'active', version: 5 }] });
  render(<Members api={api} />);
  fireEvent.click(screen.getByText('Staff management (venue administrators)'));
  fireEvent.change(await screen.findByLabelText('Cognito subject'), { target: { value: 'new-sub' } });
  fireEvent.click(screen.getByRole('button', { name: 'Add member' }));
  await screen.findByText(/Member saved. Identity sync pending/);
  expect(api).toHaveBeenCalledWith('venue', '/v1/venues/current/members', { body: { user_sub: 'new-sub', role: 'venue_staff' } });
  fireEvent.change(await screen.findByLabelText('Role for staff-sub'), { target: { value: 'venue_admin' } });
  fireEvent.change(screen.getByLabelText('Status for staff-sub'), { target: { value: 'suspended' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save member' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('venue', '/v1/venues/current/members/m1', { method: 'PATCH', version: 5, body: { role: 'venue_admin', status: 'suspended' } }));
});

it('resolves email match links across report and match pages', async () => {
  history.replaceState({}, '', '/matches/target');
  const api = vi.fn(async (_s,p) => {
    if (p === '/v1/lost-reports?limit=100&offset=0') return { items: [{ id:'closed', state:'closed' }], has_more: true };
    if (p === '/v1/lost-reports?limit=100&offset=100') return { items: [{ id:'r1', state:'open' }] };
    if (p.endsWith('offset=0')) return { items: [], has_more: true };
    return { items: [{ id: 'target', item: { item_id:'i1', title:'Notified bag', description:'Bag', category:'bag' } }] };
  });
  const choose = vi.fn(); render(<MatchLink api={api} choose={choose} revision={0} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Claim Notified bag' }));
  expect(choose).toHaveBeenCalledWith(expect.objectContaining({ item_id: 'i1' }));
  expect(api.mock.calls.some(([,p]) => p.includes('/closed/matches'))).toBe(false);
});

it('handles unavailable or foreign email matches', async () => {
  history.replaceState({}, '', '/matches/unknown');
  render(<MatchLink api={async () => ({ items: [] })} choose={vi.fn()} revision={0} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('no longer available');
  expect(screen.queryByText('This could be mine')).not.toBeInTheDocument();
});
