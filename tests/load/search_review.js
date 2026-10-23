import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { aggregateReport, casesFrom, configuration, responseValid } from './search_review_contract.js';

const config = configuration(__ENV);
if (!__ENV.SEARCH_CASES_FILE) throw Error('Set SEARCH_CASES_FILE to a reviewed query fixture');
let fixture;
try { fixture = JSON.parse(open(__ENV.SEARCH_CASES_FILE)); }
catch { throw Error('Cannot read a valid search case JSON file'); }
const cases = casesFrom(fixture);
const requests = new Counter('search_review_requests');
const valid = new Rate('search_review_valid');
const duration = new Trend('search_review_duration_ms', true);
const wall = new Trend('search_review_wall_ms', true);
const results = new Trend('search_review_results');
const thresholds = {};
for (const entry of cases) {
  thresholds[`search_review_requests{case:${entry.name}}`] = [`count>=${config.minSamples}`];
  thresholds[`search_review_valid{case:${entry.name}}`] = ['rate==1'];
  thresholds[`search_review_duration_ms{case:${entry.name}}`] = ['p(95)<=500'];
  thresholds[`search_review_results{case:${entry.name}}`] = [`min>=${entry.min_results}`];
  thresholds[`search_review_wall_ms{case:${entry.name}}`] = ['max>=0'];
}
export const options = {
  scenarios: { search_review: { executor: 'constant-vus', vus: config.vus, duration: config.duration, gracefulStop: '30s' } },
  thresholds, setupTimeout: '5m', summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
  systemTags: ['status', 'method', 'name', 'scenario', 'expected_response'],
};

function search(entry, phase) {
  const started = Date.now();
  const response = http.post(`${config.baseUrl}/v1/search`, JSON.stringify(entry.payload), {
    headers: { Authorization: `Bearer ${__ENV.LOSTLINK_ACCESS_TOKEN}`, 'Content-Type': 'application/json', 'X-LostLink-Load-Phase': phase },
    tags: { name: 'issue7-search', phase, case: entry.name }, timeout: '30s', redirects: 0,
  });
  let body = null;
  try { body = response.json(); } catch { /* Count non-JSON failures without logging their body. */ }
  const success = response.status === 200 && responseValid(body, entry, config.candidateLimit, config.modelVersion);
  if (phase === 'measure') {
    const tags = { case: entry.name };
    requests.add(1, tags); valid.add(success, tags);
    duration.add(response.timings.duration, tags); wall.add(Date.now() - started, tags);
    results.add(Array.isArray(body?.items) ? body.items.length : 0, tags);
    check(response, { 'search contract and expected results': () => success }, tags);
  }
  return success;
}

export function setup() {
  for (const entry of cases) {
    requests.add(0, { case: entry.name });
    for (let index = 0; index < config.warmup; index++) {
      if (!search(entry, 'warmup')) throw Error('Warmup failed contract/expected-result checks; no benchmark was run');
    }
  }
}

export default function () {
  const entry = cases[(__ITER + __VU - 1) % cases.length];
  search(entry, 'measure');
  // Small closed-model backend investigation; Issue #10 owns the 200-VU scaling demo.
  sleep(0.01);
}

export function handleSummary(data) {
  const report = aggregateReport(data, cases, config);
  const output = { stdout: JSON.stringify(report, null, 2) + '\n' };
  if (__ENV.SEARCH_SUMMARY_FILE) output[__ENV.SEARCH_SUMMARY_FILE] = JSON.stringify(report, null, 2) + '\n';
  return output;
}
