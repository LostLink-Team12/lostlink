import React from 'react';

type Tone = 'info' | 'success' | 'warning' | 'danger' | 'neutral' | 'reserved';
const statuses: Record<string, { label: string; tone: Tone }> = {
  open: { label: 'Open', tone: 'info' }, submitted: { label: 'Submitted', tone: 'info' },
  claim_submitted: { label: 'Claim submitted', tone: 'info' },
  awaiting_upload: { label: 'Awaiting photo', tone: 'warning' }, processing: { label: 'Processing', tone: 'warning' },
  approval_pending: { label: 'Approval pending', tone: 'warning' },
  cancellation_pending: { label: 'Withdrawal pending', tone: 'warning' },
  handover_pending: { label: 'Handover pending', tone: 'warning' }, pending: { label: 'Pending', tone: 'warning' },
  available: { label: 'Available', tone: 'success' }, approved: { label: 'Approved', tone: 'success' },
  handed_over: { label: 'Handed over', tone: 'success' }, active: { label: 'Active', tone: 'success' },
  completed: { label: 'Completed', tone: 'success' }, reserved: { label: 'Reserved', tone: 'reserved' },
  rejected: { label: 'Rejected', tone: 'danger' }, processing_failed: { label: 'Processing failed', tone: 'danger' },
  failed: { label: 'Failed', tone: 'danger' }, suspended: { label: 'Suspended', tone: 'danger' },
  needs_attention: { label: 'Needs attention', tone: 'danger' },
  cancelled: { label: 'Cancelled', tone: 'neutral' }, closed: { label: 'Closed', tone: 'neutral' },
  withdrawn: { label: 'Withdrawn', tone: 'neutral' }, superseded: { label: 'Superseded', tone: 'neutral' },
};

export function StatusBadge({ state }: { state: string }) {
  const status = statuses[state] || { label: state.replaceAll('_', ' '), tone: 'neutral' };
  return <span className={`status-badge status-${status.tone}`} data-state={state} aria-label={`Status: ${status.label}`}>
    <span className="status-dot" aria-hidden="true" />{status.label}
  </span>;
}
