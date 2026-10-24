import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { OwnerApp } from '../src/App';

it('separates owner pages, loads records on demand and retains the search draft', async () => {
  const api = vi.fn(async () => ({ items: [] }));
  render(<OwnerApp api={api} />);
  fireEvent.change(screen.getByLabelText('Describe your lost item'), { target: { value: 'Blue mug' } });
  expect(api).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('link', { name: /My lost reports/ }));
  await screen.findByText('No records yet.');
  expect(screen.getByRole('heading', { name: 'My lost reports' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Search items' })).not.toBeInTheDocument();
  expect(api).toHaveBeenCalledWith('matching', '/v1/lost-reports?limit=20&offset=0');
  fireEvent.click(screen.getByRole('link', { name: /My claims/ }));
  await screen.findByRole('heading', { name: 'My claims' });
  expect(screen.queryByRole('heading', { name: 'My lost reports' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('link', { name: /Search/ }));
  expect(screen.getByLabelText('Describe your lost item')).toHaveValue('Blue mug');
  expect(screen.getByRole('link', { name: /Search/ })).toHaveAttribute('aria-current', 'page');
});
