import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import { OwnerApp, PageList } from '../src/App';

it('pages similar items independently of the report list and resets when selecting another report', async () => {
  history.replaceState({}, '', '/owner-app/#reports');
  const items = Array.from({ length: 25 }, (_, i) => ({ item_id: `i${i}`, title: `Item ${i}`, description: 'Blue bottle', category: 'Bottle' }));
  const api = vi.fn(async (_s, path) => {
    if (path.startsWith('/v1/lost-reports?')) return { items: [{ id: 'r1', description: 'First bottle', state: 'open' }, { id: 'r2', description: 'Second bottle', state: 'open' }], has_more: false };
    if (path.includes('/candidates?')) {
      const offset = Number(new URL(path, 'https://test.invalid').searchParams.get('offset'));
      const rows = path.includes('/r2/') ? items.slice(0, 1) : items;
      return { items: rows.slice(offset, offset + 20), has_more: offset + 20 < rows.length };
    }
    return { items: [] };
  });
  render(<OwnerApp api={api} />);
  fireEvent.click(within(await screen.findByRole('article', { name: 'Lost report: First bottle' })).getByRole('button', { name: 'View matches' }));
  const region = await screen.findByRole('region', { name: 'Report matches' });
  await within(region).findByRole('heading', { name: 'Item 0', exact: true });
  const pages = within(region).getByRole('navigation', { name: 'Similar item pages' });
  expect(within(pages).getByRole('button', { name: 'Next' })).toBeEnabled();
  expect(within(screen.getByRole('navigation', { name: 'Lost report pages' })).getByRole('button', { name: 'Next' })).toBeDisabled();
  fireEvent.click(within(pages).getByRole('button', { name: 'Next' }));
  await within(region).findByRole('heading', { name: 'Item 20', exact: true });
  expect(within(pages).getByText('Showing 21–25')).toBeInTheDocument();
  expect(within(pages).getByRole('button', { name: 'Next' })).toBeDisabled();
  expect(within(pages).getByRole('button', { name: 'Previous' })).toBeEnabled();
  fireEvent.click(within(screen.getByRole('article', { name: 'Lost report: Second bottle' })).getByRole('button', { name: 'View matches' }));
  await within(region).findByRole('heading', { name: 'Item 0', exact: true });
  expect(api).toHaveBeenCalledWith('matching', '/v1/lost-reports/r2/candidates?limit=20&offset=0');
  expect(api.mock.calls.some(([, p]) => p.includes('/r2/') && p.includes('offset=20'))).toBe(false);
  expect(within(region).getByText('Showing 1–1')).toBeInTheDocument();
});

it('disables navigation only while a page request is pending and prevents double advances', async () => {
  let complete!: (page: any) => void;
  const api = vi.fn(async (_s, path) => path.endsWith('offset=0') ? { items: [{ title: 'First page' }], has_more: true } : new Promise(resolve => { complete = resolve; }));
  render(<PageList api={api} service="matching" path="/reports/r1" label="Test pages">{rows => rows.map(row => <p key={row.title}>{row.title}</p>)}</PageList>);
  await screen.findByText('First page');
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  await waitFor(() => expect(api).toHaveBeenCalledTimes(2));
  expect(screen.getByRole('navigation', { name: 'Test pages' })).toHaveAttribute('aria-busy', 'true');
  expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
  expect(screen.getByText('Page 1')).toBeInTheDocument();
  expect(screen.getByText('Showing 1–1')).toBeInTheDocument();
  const next = screen.getByRole('button', { name: 'Next' });
  expect(next).toBeDisabled(); fireEvent.click(next);
  expect(api).toHaveBeenCalledTimes(2);
  complete({ items: [{ title: 'Last page' }], has_more: false });
  await screen.findByText('Last page');
  expect(screen.getByRole('navigation', { name: 'Test pages' })).toHaveAttribute('aria-busy', 'false');
  expect(screen.getByRole('button', { name: 'Previous' })).toBeEnabled();
});

it('retries a failed next page without advancing past it or relabelling old records', async () => {
  let failed = false;
  const api = vi.fn(async (_s, path) => {
    if (path.endsWith('offset=0')) return { items: [{ title: 'Original page' }], has_more: true };
    if (!failed) { failed = true; throw Error('Network interrupted'); }
    return { items: [{ title: 'Recovered next page' }], has_more: false };
  });
  render(<PageList api={api} service="item" path="/v1/items">{rows => rows.map(row => <p key={row.title}>{row.title}</p>)}</PageList>);
  await screen.findByText('Original page');
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Network interrupted');
  expect(screen.getByText('Page 1')).toBeInTheDocument();
  expect(screen.getByText('Showing 1–1')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  await screen.findByText('Recovered next page');
  expect(api.mock.calls.map(([, path]) => path)).toEqual(['/v1/items?limit=20&offset=0', '/v1/items?limit=20&offset=20', '/v1/items?limit=20&offset=20']);
  expect(screen.getByText('Page 2')).toBeInTheDocument();
});

it('can retry an initial list failure from its own error state', async () => {
  const api = vi.fn().mockRejectedValueOnce(Error('Service unavailable')).mockResolvedValue({ items: [{ title: 'Loaded after retry' }], has_more: false });
  render(<PageList api={api} service="claim" path="/v1/claims">{rows => rows.map(row => <p key={row.title}>{row.title}</p>)}</PageList>);
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'Retry loading records' }));
  await screen.findByText('Loaded after retry');
  expect(api).toHaveBeenCalledTimes(2);
});
