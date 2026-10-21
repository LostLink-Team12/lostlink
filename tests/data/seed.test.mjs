import { test } from 'node:test';
import assert from 'node:assert/strict';
import { selectItems, importItems } from './seed.mjs';

test('selects unique portable items deterministically, excludes unknown licenses, retains attribution', () => {
  const images = ['0000000000000001', '0000000000000002', '0000000000000003'].map((ImageID, i) => ({ ImageID, Author: 'Creator', Title: 'Photo', OriginalLandingURL: 'https://flickr.com/photo', License: i === 1 ? 'unknown' : 'https://creativecommons.org/licenses/by/2.0/' }));
  const labels = images.flatMap(x => [{ ImageID: x.ImageID, LabelName: '/bag', Confidence: '1' }, { ImageID: x.ImageID, LabelName: '/bag', Confidence: '1' }]);
  const rows = selectItems(images, labels, new Map([['/bag', 'Backpack']]), 2);
  assert.equal(rows.length, 2);
  assert.equal(new Set(rows.map(x => x.client_reference)).size, 2);
  assert.equal(rows[0].attribution.author, 'Creator');
  assert.equal(rows[0].item.category, 'Backpack');
  assert.throws(() => selectItems(images, labels, new Map([['/bag', 'Backpack']]), 3), /Only 2/);
});

test('imports in API-sized batches, uploads once, polls confirmation and replays safely', async () => {
  const calls = [];
  const row = { client_reference: 'oi-one', photo_path: 'photos/one.jpg', item: { title: 'Backpack' } };
  const api = async (_service, path, options) => {
    calls.push({ path, options });
    if (path === '/v1/item-imports') return { id: 'job' };
    if (path.startsWith('/v1/item-imports/job')) return { job: { accepted: 1, failed: 0, total: 1 }, items: [{ state: 'succeeded', client_reference: 'oi-one', item_id: 'one' }] };
    if (path === '/v1/items/one') return { state: 'awaiting_upload', version: 2 };
    if (path === '/v1/items/one/uploads') return { upload_id: 'up', upload: { url: 'https://s3.test', fields: {} } };
    if (path.endsWith('/confirm')) return { operation_id: 'op' };
    if (path === '/v1/item-operations/op') return { state: 'completed' };
    throw Error(path);
  };
  let uploaded = 0;
  await importItems([row], api, async () => { uploaded++; }, async () => new Blob(['jpeg'], { type: 'image/jpeg' }));
  assert.equal(uploaded, 1);
  assert.equal(calls.find(x => x.path.endsWith('/uploads')).options.version, 2);
  const first = calls[0].options.key;
  assert.deepEqual(calls[0].options.body.entries, [{ client_reference: 'oi-one', title: 'Backpack' }]);
  calls.length = 0;
  await importItems([row], api, async () => {}, async () => new Blob(['jpeg'], { type: 'image/jpeg' }));
  assert.equal(calls[0].options.key, first);
});

test('skips photos already confirmed on a resumed import', async () => {
  const rows = [{ client_reference: 'one', item: { title: 'Bag' } }];
  const api = async (_service, path) => {
    if (path === '/v1/item-imports') return { id: 'job' };
    if (path.startsWith('/v1/item-imports/job')) return { job: { accepted: 1, failed: 0, total: 1 }, items: [{ client_reference: 'one', state: 'succeeded', item_id: 'item' }] };
    if (path === '/v1/items/item') return { state: 'available' };
    throw Error('Unexpected write');
  };
  const forbidden = async () => { throw Error('Must not read or upload photos again'); };
  assert.equal(await importItems(rows, api, forbidden, forbidden), 1);
});

test('stops on failed import rows without uploading photos', async () => {
  const api = async (_service, path) => path === '/v1/item-imports' ? { id: 'job' } : { job: { accepted: 0, failed: 1, total: 1 }, items: [{ state: 'failed' }] };
  await assert.rejects(importItems([{ client_reference: 'one', item: {} }], api, async () => { throw Error('Must not upload'); }), /failed or incomplete/);
});
