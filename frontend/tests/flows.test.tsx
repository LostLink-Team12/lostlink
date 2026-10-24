import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { OwnerApp, VenueApp } from '../src/App';

it('owner searches, answers public verification questions and submits a claim', async () => {
  const api = vi.fn(async (_service, path, options) => {
    if (path === '/v1/search') return { items: [{ item_id: 'item-1', title: 'Blue bag', description: 'Backpack', category: 'bag', score: 0.62 }], potentially_truncated: false };
    if (path.startsWith('/v1/claim-questions')) return { item_id: 'item-1', title: 'Blue bag', questions: [{ id: 'q1', version: 2, prompt: 'What is inside?' }] };
    if (path === '/v1/claims' && options?.body) return { id: 'c1', item_title: 'Blue bag', state: 'submitted', version: 1 };
    return { items: [] };
  });
  render(<OwnerApp api={api} />);
  fireEvent.change(screen.getByLabelText('Describe your lost item'), { target: { value: 'blue bag' } });
  fireEvent.click(screen.getByRole('button', { name: 'Search items' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Claim Blue bag' }));
  fireEvent.change(await screen.findByLabelText('What is inside?'), { target: { value: 'A red notebook' } });
  fireEvent.click(screen.getByRole('button', { name: 'Submit claim' }));
  await screen.findByText('Claim submitted. The venue will review your answers.');
  expect(api).toHaveBeenCalledWith('claim', '/v1/claims', expect.objectContaining({ body: { item_id: 'item-1', answers: [{ question_id: 'q1', version: 2, answer: 'A red notebook' }] } }));
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  expect(screen.getByText('Similarity 0.620')).toBeInTheDocument();
  expect(location.hash).toBe('#search');
});

it('venue loads private answers, sends If-Match and waits for approval completion', async () => {
  const api = vi.fn(async (_service, path) => {
    if (path === '/v1/session') return { venue: { name: 'Campus' }, can_manage_venue: true, can_manage_members: true, can_onboard_venues: false };
    if (path.startsWith('/v1/items?')) return { items: [] };
    if (path.startsWith('/v1/venue/claims?')) return { items: [{ id: 'c1', item_title: 'Blue bag', state: 'submitted' }] };
    if (path === '/v1/venue/claims/c1') return { id: 'c1', item_title: 'Blue bag', state: 'submitted', version: 4, answers: [{ question_id: 'q1', prompt: 'Inside?', answer: 'Notebook' }] };
    if (path.endsWith('/approve')) return { operation_id: 'op1' };
    if (path === '/v1/claim-operations/op1') return { state: 'completed', claim_state: 'approved' };
    return { items: [] };
  });
  render(<VenueApp api={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review Blue bag' }));
  await screen.findByText('Notebook');
  fireEvent.click(screen.getByRole('button', { name: 'Approve claim' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('claim', '/v1/claim-operations/op1'));
  expect(api).toHaveBeenCalledWith('claim', '/v1/venue/claims/c1/approve', expect.objectContaining({ version: 4 }));
  await screen.findByText('Claim approved.');
});

it('reports API errors visibly and keeps the action retryable', async () => {
  render(<OwnerApp api={async (_s, path) => { if (path === '/v1/search') throw Error('Search unavailable'); return { items: [] }; }} />);
  fireEvent.change(screen.getByLabelText('Describe your lost item'), { target: { value: 'bag' } });
  fireEvent.click(screen.getByRole('button', { name: 'Search items' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Search unavailable');
  expect(screen.getByRole('button', { name: 'Search items' })).toBeEnabled();
});

it('saves a lost report using description instead of the search-only text field', async () => {
  const api = vi.fn(async () => ({ items: [] }));
  render(<OwnerApp api={api} />);
  fireEvent.change(screen.getByLabelText('Describe your lost item'), { target: { value: 'Black umbrella' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save lost report' }));
  await screen.findByText('Lost report saved. View it in My lost reports.');
  expect(api).toHaveBeenCalledWith('matching', '/v1/lost-reports', { body: { description: 'Black umbrella' } });
});


it('opens and focuses the claim panel immediately while questions load, preventing duplicate requests', async () => {
  let completeQuestions!: (value: any) => void;
  const questions = new Promise(resolve => { completeQuestions = resolve; });
  const api = vi.fn(async (_service, path) => {
    if (path === '/v1/search') return { items: [{ item_id: 'item-1', title: 'Blue bag', description: 'Backpack', category: 'bag' }] };
    if (path.startsWith('/v1/claim-questions')) return questions;
    return { items: [] };
  });
  render(<OwnerApp api={api} />);
  fireEvent.change(screen.getByLabelText('Describe your lost item'), { target: { value: 'bag' } });
  fireEvent.click(screen.getByRole('button', { name: 'Search items' }));
  const trigger = await screen.findByRole('button', { name: 'Claim Blue bag' });
  fireEvent.click(trigger);
  const panel = await screen.findByRole('region', { name: 'Claim Blue bag' });
  expect(within(panel).getByRole('heading')).toHaveFocus();
  expect(within(panel).getByRole('status')).toHaveTextContent('Working');
  expect(screen.queryByRole('heading', { name: 'Search results' })).not.toBeInTheDocument();
  expect(location.hash).toBe('#claim/item-1');
  expect(screen.getAllByText('Backpack').some(node => !node.closest('[hidden]'))).toBe(true);
  expect(trigger).toBeDisabled();
  fireEvent.click(trigger);
  expect(api.mock.calls.filter(([, path]) => path.startsWith('/v1/claim-questions'))).toHaveLength(1);
  completeQuestions({ item_id: 'item-1', title: 'Blue bag', questions: [{ id: 'q1', version: 1, prompt: 'Inside?' }] });
  await within(panel).findByLabelText('Inside?');
  fireEvent.click(within(panel).getByRole('button', { name: 'Cancel' }));
  expect(screen.queryByRole('region', { name: 'Claim Blue bag' })).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Search results' })).toBeInTheDocument();
  expect(screen.getByLabelText('Describe your lost item')).toHaveValue('bag');
});

it('shows question-loading errors in the claim panel and allows retry', async () => {
  let attempts = 0;
  const api = vi.fn(async (_service, path) => {
    if (path === '/v1/search') return { items: [{ item_id: 'item-1', title: 'Blue bag', description: 'Backpack', category: 'bag' }] };
    if (path.startsWith('/v1/claim-questions')) {
      if (++attempts === 1) throw Error('This item cannot be claimed now');
      return { item_id: 'item-1', title: 'Blue bag', questions: [{ id: 'q1', version: 2, prompt: 'Inside?' }] };
    }
    return { items: [] };
  });
  render(<OwnerApp api={api} />);
  fireEvent.change(screen.getByLabelText('Describe your lost item'), { target: { value: 'bag' } });
  fireEvent.click(screen.getByRole('button', { name: 'Search items' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Claim Blue bag' }));
  const panel = await screen.findByRole('region', { name: 'Claim Blue bag' });
  expect(await within(panel).findByRole('alert')).toHaveTextContent('This item cannot be claimed now');
  fireEvent.click(within(panel).getByRole('button', { name: 'Retry loading questions' }));
  await within(panel).findByLabelText('Inside?');
  expect(attempts).toBe(2);
  expect(within(panel).queryByRole('alert')).not.toBeInTheDocument();
});

it('keeps claim answers and submission errors together so the owner can retry', async () => {
  let submissions = 0;
  const api = vi.fn(async (_service, path, options) => {
    if (path === '/v1/search') return { items: [{ item_id: 'item-1', title: 'Blue bag', description: 'Backpack', category: 'bag' }] };
    if (path.startsWith('/v1/claim-questions')) return { item_id: 'item-1', title: 'Blue bag', questions: [{ id: 'q1', version: 2, prompt: 'Inside?' }] };
    if (path === '/v1/claims' && options?.body && ++submissions === 1) throw Error('Please try again');
    return { items: [] };
  });
  render(<OwnerApp api={api} />);
  fireEvent.change(screen.getByLabelText('Describe your lost item'), { target: { value: 'bag' } });
  fireEvent.click(screen.getByRole('button', { name: 'Search items' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Claim Blue bag' }));
  const answer = await screen.findByLabelText('Inside?');
  fireEvent.change(answer, { target: { value: 'Notebook' } });
  fireEvent.click(screen.getByRole('button', { name: 'Submit claim' }));
  const panel = screen.getByRole('region', { name: 'Claim Blue bag' });
  expect(await within(panel).findByRole('alert')).toHaveTextContent('Please try again');
  expect(answer).toHaveValue('Notebook');
  fireEvent.click(within(panel).getByRole('button', { name: 'Submit claim' }));
  await within(panel).findByText('Claim submitted. The venue will review your answers.');
  expect(submissions).toBe(2);
  expect(within(panel).queryByRole('button', { name: 'Submit claim' })).not.toBeInTheDocument();
  expect(within(panel).queryByRole('button', { name: 'Retry loading questions' })).not.toBeInTheDocument();
});
