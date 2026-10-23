import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { ItemCards } from '../src/App';

const item = { item_id: 'item-1', title: 'Mug', description: 'A mug', category: 'Mug', photo_url: 'https://photos.invalid/expired' };

it('refreshes an expired lazy image through the authenticated API without repeating search', async () => {
  const api = vi.fn(async () => ({ item_id: 'item-1', photo_url: 'https://photos.invalid/fresh' }));
  render(<ItemCards items={[item]} api={api} />);
  fireEvent.error(screen.getByRole('img', { name: 'Mug' }));
  await waitFor(() => expect(screen.getByRole('img', { name: 'Mug' })).toHaveAttribute('src', 'https://photos.invalid/fresh'));
  expect(api).toHaveBeenCalledExactlyOnceWith('matching', '/v1/items/item-1/photo');
  fireEvent.error(screen.getByRole('img', { name: 'Mug' }));
  await screen.findByText('Photo unavailable.');
  expect(api).toHaveBeenCalledTimes(1);
});

it('shows a fallback for a removed item and allows an explicit retry', async () => {
  const api = vi.fn().mockRejectedValueOnce(Error('Item unavailable')).mockResolvedValueOnce({ item_id: 'item-1', photo_url: 'https://photos.invalid/fresh' });
  render(<ItemCards items={[item]} api={api} />);
  fireEvent.error(screen.getByRole('img', { name: 'Mug' }));
  await screen.findByText('Photo unavailable.');
  fireEvent.click(screen.getByRole('button', { name: 'Retry photo for Mug' }));
  await waitFor(() => expect(screen.getByRole('img', { name: 'Mug' })).toHaveAttribute('src', 'https://photos.invalid/fresh'));
  expect(api).toHaveBeenCalledTimes(2);
});

it('does not loop when the service returns the same URL or a different item', async () => {
  const api = vi.fn(async () => ({ item_id: 'item-1', photo_url: item.photo_url }));
  render(<ItemCards items={[item]} api={api} />);
  fireEvent.error(screen.getByRole('img', { name: 'Mug' }));
  await screen.findByText('Photo unavailable.');
  expect(api).toHaveBeenCalledTimes(1);
});

it('deduplicates concurrent errors and discards a response after the card is replaced', async () => {
  let complete!: (value: any) => void;
  const api = vi.fn(() => new Promise(resolve => { complete = resolve; }));
  const { rerender } = render(<ItemCards items={[item]} api={api} />);
  const image = screen.getByRole('img', { name: 'Mug' });
  fireEvent.error(image); fireEvent.error(image);
  expect(api).toHaveBeenCalledTimes(1);
  rerender(<ItemCards items={[{ ...item, photo_url: 'https://photos.invalid/new-search' }]} api={api} />);
  complete({ item_id: 'item-1', photo_url: 'https://photos.invalid/stale-refresh' });
  await waitFor(() => expect(screen.getByRole('img', { name: 'Mug' })).toHaveAttribute('src', 'https://photos.invalid/new-search'));
});
