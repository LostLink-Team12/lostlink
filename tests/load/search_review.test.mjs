import assert from 'node:assert/strict';
import test from 'node:test';
import { aggregateReport, casesFrom, configuration, responseValid } from './search_review_contract.js';

const id = '00000000-0000-0000-0000-000000000001';
const tenant = '00000000-0000-0000-0000-000000000002';
const other = '00000000-0000-0000-0000-000000000003';
const fixture = () => ({ cases: [{ name: 'text', payload: { text: 'private query', limit: 1 }, expected_item_ids: [id] }] });
const entry = () => casesFrom(fixture())[0];
const response = () => ({
  items: [{ item_id: id, tenant_id: tenant, item_version: 1, title: 'Test', description: '',
    category: 'Bags', found_at: '2026-01-01T00:00:00Z', photo_url: null,
    public_location_hint: null, score: 0.8 }],
  limit: 1, candidate_limit: 100, model_version: 'pinned', potentially_truncated: false,
});
const env = { MATCHING_API_URL: 'https://matching.example', LOSTLINK_ACCESS_TOKEN: 'private-token' };

test('text, photo and fusion cases require known positive results', () => {
  for (const payload of [{ text: 'bag' }, { photo_base64: 'YWJj' }, { text: 'bag', photo_base64: 'YWJj' }]) {
    const value = fixture(); value.cases[0].payload = { ...payload, limit: 100 };
    assert.equal(casesFrom(value).length, 1);
  }
  const value = fixture(); value.cases[0].expected_item_ids = [];
  assert.throws(() => casesFrom(value));
});

test('reject unsupported payloads, invalid limits, dates and overlapping expectations', () => {
  for (const change of [{ candidate_limit: 20 }, { limit: 0 }, { limit: 101 },
    { min_score: NaN }, { found_from: 'bad' }, { photo_base64: 'data:image/png,abc' },
    { found_from: '2026-02-01Z', found_until: '2026-01-01Z' }]) {
    const value = fixture(); Object.assign(value.cases[0].payload, change);
    assert.throws(() => casesFrom(value));
  }
  const value = fixture(); value.cases[0].forbidden_item_ids = [id];
  assert.throws(() => casesFrom(value));
  assert.throws(() => casesFrom({ cases: [entry(), entry()] }));
  for (const name of [undefined, null, '', 'private label with spaces']) {
    const value = fixture(); value.cases[0].name = name;
    assert.throws(() => casesFrom(value));
  }
});

test('validate full results, server candidate bound and model version', () => {
  assert.equal(responseValid(response(), entry(), 100, 'pinned'), true);
  for (const change of [{ items: [] }, { items: [null] }, { candidate_limit: 20 },
    { model_version: 'other' }, { limit: 2 }, { potentially_truncated: 'false' }]) {
    assert.equal(responseValid({ ...response(), ...change }, entry(), 100, 'pinned'), false);
  }
  for (const change of [{ item_id: other }, { tenant_id: 'bad' }, { item_version: 0 },
    { score: NaN }, { score: 1.1 }, { found_at: 'bad' }, { photo_url: undefined }]) {
    const body = response(); Object.assign(body.items[0], change);
    assert.equal(responseValid(body, entry(), 100, 'pinned'), false);
  }
});

test('validate filters and explicitly forbidden stale hits', () => {
  const selected = entry();
  Object.assign(selected.payload, { category: 'Bags', venue_id: tenant,
    found_from: '2025-01-01T00:00:00Z', found_until: '2027-01-01T00:00:00Z', min_score: 0.5 });
  assert.equal(responseValid(response(), selected, 100), true);
  for (const change of [{ category: 'Keys' }, { tenant_id: other }, { score: 0.4 },
    { found_at: '2028-01-01T00:00:00Z' }]) {
    const body = response(); Object.assign(body.items[0], change);
    assert.equal(responseValid(body, selected, 100), false);
  }
  selected.payload.limit = 2; selected.forbidden_item_ids = [other];
  const body = response(); body.limit = 2;
  body.items.push({ ...body.items[0], item_id: other });
  assert.equal(responseValid(body, selected, 100), false);
  body.items[1].item_id = id;
  assert.equal(responseValid(body, selected, 100), false);
});

test('measurement enforces duration and samples; loopback requires explicit fixture scope', () => {
  assert.equal(configuration(env).minSamples, 100);
  assert.equal(configuration(env).vus, 1);
  for (const change of [{ SEARCH_DURATION: '5s' }, { SEARCH_MIN_SAMPLES: '10' },
    { SEARCH_VUS: '200' }, { MATCHING_API_URL: 'http://127.0.0.1:9000' },
    { MATCHING_API_URL: 'https://secret@matching.example' }, { LOSTLINK_ACCESS_TOKEN: '' }]) {
    assert.throws(() => configuration({ ...env, ...change }));
  }
  assert.equal(configuration({ ...env, SEARCH_PROFILE: 'validation', SEARCH_DURATION: '1s',
    SEARCH_SCOPE: 'local-fixture', MATCHING_API_URL: 'http://127.0.0.1:9000' }).minSamples, 5);
});

test('aggregate evidence fails insufficient, invalid and slow samples and redacts inputs', () => {
  const config = configuration(env);
  function report(count = 100, rate = 1, p95 = 400) {
    const metrics = {};
    for (const [name, values] of Object.entries({ requests: { count }, valid: { rate },
      duration_ms: { 'p(95)': p95, 'p(99)': p95 + 10 }, results: { min: 1, max: 1 } })) {
      metrics[`search_review_${name}{case:text}`] = { values };
    }
    return aggregateReport({ metrics }, [entry()], config);
  }
  assert.equal(report().passed, true);
  assert.equal(report(99).cases[0].decision, 'insufficient-samples');
  assert.equal(report(100, 0.99).cases[0].decision, 'invalid-responses');
  assert.equal(report(100, 1, 501).cases[0].decision, 'investigate-latency');
  assert.equal(report(100, 1, 500).passed, true);
  const output = JSON.stringify(report());
  for (const secret of [id, tenant, 'private query', 'private-token']) assert.ok(!output.includes(secret));
  assert.equal(aggregateReport({ metrics: {} }, [entry()], config).passed, false);
  assert.equal(aggregateReport({ metrics: {} }, [entry()], { ...config, scope: 'local-fixture' }).acceptance_eligible, false);
});
