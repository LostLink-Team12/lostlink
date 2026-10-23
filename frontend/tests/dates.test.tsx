import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { OwnerApp } from '../src/App';
import { dateRangeMessage, foundDateRange } from '../src/SearchDates';
import { createApi } from '../src/api.js';

it.each(['Search items', 'Save lost report'])('rejects reversed dates next to the fields without requesting %s', async button => {
  const api = vi.fn(async () => ({ items: [] }));
  render(<OwnerApp api={api} />);
  fireEvent.change(screen.getByLabelText('Describe your lost item'), { target: { value: 'bag' } });
  fireEvent.change(screen.getByLabelText('Found from'), { target: { value: '2026-10-24T10:00' } });
  fireEvent.change(screen.getByLabelText('Found until'), { target: { value: '2026-10-23T10:00' } });
  expect(await screen.findByRole('alert')).toHaveTextContent(dateRangeMessage);
  expect(screen.getByLabelText('Found from')).toHaveAttribute('aria-invalid', 'true');
  expect(screen.getByLabelText('Found until')).toHaveAccessibleDescription(dateRangeMessage);
  fireEvent.click(screen.getByRole('button', { name: button }));
  expect(screen.getByLabelText('Found from').closest('details')).toHaveAttribute('open');
  expect(screen.getByLabelText('Found from')).toHaveFocus();
  expect(api.mock.calls.every(([, path]) => path.includes('?limit='))).toBe(true);
  fireEvent.change(screen.getByLabelText('Found until'), { target: { value: '2026-10-25T10:00' } });
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: button }));
  await screen.findByText(button === 'Search items' ? 'No matches. Try another description or save a lost report.' : 'Lost report saved. New matches will appear below.');
  expect(api).toHaveBeenCalledWith('matching', button === 'Search items' ? '/v1/search' : '/v1/lost-reports', expect.objectContaining({ body: expect.objectContaining(foundDateRange('2026-10-24T10:00', '2026-10-25T10:00')) }));
});

it('accepts omitted, one-sided and equal bounds and converts local dates to UTC', () => {
  expect(foundDateRange('', '')).toEqual({});
  expect(foundDateRange('2026-10-23T10:00', '')).toEqual({ found_from: new Date('2026-10-23T10:00').toISOString() });
  expect(foundDateRange('', '2026-10-23T10:00')).toHaveProperty('found_until');
  const equal = foundDateRange('2026-10-23T10:00', '2026-10-23T10:00');
  expect(equal.found_from).toBe(equal.found_until);
  expect(() => foundDateRange('not-a-date', '')).toThrow('Found from must be a valid date');
});

it.each([
  [[{ location: ['body'], type: 'invalid_date_range' }], dateRangeMessage],
  [[{ location: ['body', 'found_until'], type: 'timezone_aware' }], 'Found until must be a valid date and time with a timezone.'],
])('translates server date validation into readable messages without echoing submitted values', async (details, message) => {
  const api = createApi({ matching: '/api/matching' }, () => 'test-token', vi.fn(async () => ({ ok: false, status: 422, json: async () => ({ error: { code: 'validation_error', message: 'Request validation failed', details } }) })));
  await expect(api('matching', '/v1/search', { body: { text: 'bag' } })).rejects.toMatchObject({ status: 422, message, details });
});
