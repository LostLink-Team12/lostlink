import React from 'react';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { OwnerApp } from '../src/App';

it.each([false, true])('persists claim provenance after question retry=%s and restores the marker after remount', async retry => {
  let submitted = false, questionAttempts = 0;
  const api = vi.fn(async (_s, path, options) => {
    if (path.startsWith('/v1/lost-reports?')) return { items: [{ id: 'report-1', description: 'Blue bag report', state: 'open', version: 1 }] };
    if (path.includes('/candidates?')) return { items: [{ item_id: 'item-1', title: 'Blue bag', description: 'Blue backpack', category: 'bag' }] };
    if (path.startsWith('/v1/claim-questions') && retry && ++questionAttempts === 1) throw Error('Try again');
    if (path.startsWith('/v1/claim-questions')) return { item_id: 'item-1', questions: [{ id: 'q1', version: 1, prompt: 'Inside?' }] };
    if (path === '/v1/claims' && options?.body) { submitted = true; return { id: 'claim-1', state: 'submitted' }; }
    if (path.startsWith('/v1/claims?lost_report_id=')) return { items: submitted ? [{ id: 'claim-1', state: 'submitted', lost_report_id: 'report-1' }] : [] };
    return { items: [] };
  });
  history.replaceState({}, '', '/owner-app/#reports');
  render(<OwnerApp api={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'View matches' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Claim Blue bag' }));
  if (retry) fireEvent.click(await screen.findByRole('button', { name: 'Retry loading questions' }));
  fireEvent.change(await screen.findByLabelText('Inside?'), { target: { value: 'Notebook' } });
  fireEvent.click(screen.getByRole('button', { name: 'Submit claim' }));
  await screen.findByText('Claim submitted. The venue will review your answers.');
  expect(api).toHaveBeenCalledWith('claim', '/v1/claims', expect.objectContaining({ body: expect.objectContaining({ lost_report_id: 'report-1' }) }));
  cleanup(); history.replaceState({}, '', '/owner-app/#reports');
  render(<OwnerApp api={api} />);
  expect(await screen.findByText('Claim submitted', { exact: true })).toBeInTheDocument();
  expect(screen.getByLabelText('Status: Open')).toBeInTheDocument();
});
