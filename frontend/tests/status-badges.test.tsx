import React from 'react';
import { render, screen } from '@testing-library/react';
import { it, expect } from 'vitest';
import { StatusBadge } from '../src/StatusBadge';

it.each([
  ['open', 'Open', 'info'], ['claim_submitted', 'Claim submitted', 'info'],
  ['approval_pending', 'Approval pending', 'warning'], ['handover_pending', 'Handover pending', 'warning'],
  ['cancellation_pending', 'Withdrawal pending', 'warning'], ['available', 'Available', 'success'],
  ['approved', 'Approved', 'success'], ['handed_over', 'Handed over', 'success'],
  ['reserved', 'Reserved', 'reserved'], ['processing_failed', 'Processing failed', 'danger'],
  ['rejected', 'Rejected', 'danger'], ['suspended', 'Suspended', 'danger'],
  ['cancelled', 'Cancelled', 'neutral'], ['closed', 'Closed', 'neutral'],
  ['future_state', 'future state', 'neutral'],
])('provides a readable accessible label and distinct color for %s', (state, label, tone) => {
  render(<StatusBadge state={state} />);
  const badge = screen.getByLabelText(`Status: ${label}`);
  expect(badge).toHaveTextContent(label);
  expect(badge).toHaveClass(`status-${tone}`);
  expect(badge).toHaveAttribute('data-state', state);
  expect(badge).not.toHaveAttribute('role', 'status');
});
