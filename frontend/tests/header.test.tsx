import React from 'react';
import { render, screen } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { PortalHeader } from '../src/PortalHeader';

it.each([false, true])('shows the same noninteractive email before portal links; venue=%s', isVenue => {
  render(<PortalHeader signedIn email="owner@example.com" isVenue={isVenue} signOut={vi.fn()} />);
  const email = screen.getByLabelText('Signed-in email');
  expect(email).toHaveTextContent('owner@example.com');
  expect(email.tagName).toBe('SPAN');
  expect(email.closest('a, button')).toBeNull();
  expect(screen.getByRole('link', { name: isVenue ? 'Venue Portal' : 'Owner App' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('link', { name: isVenue ? 'Owner App' : 'Venue Portal' })).not.toHaveAttribute('aria-current');
  expect(email.compareDocumentPosition(screen.getByRole('link', { name: 'Owner App' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});
