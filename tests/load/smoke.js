import http from 'k6/http';
import { check, sleep } from 'k6';

// Read-only against dev: never creates claims or mutates seeded inventory.
export const options = {
  vus: 1, iterations: 5,
  thresholds: { http_req_failed: ['rate<0.01'], http_req_duration: ['p(95)<5000'], checks: ['rate==1'] },
};

export function setup() {
  if (!__ENV.MATCHING_API_URL || !__ENV.LOSTLINK_ACCESS_TOKEN) throw Error('Set MATCHING_API_URL and LOSTLINK_ACCESS_TOKEN (owner access token)');
}

export default function () {
  const body = { text: __ENV.SEARCH_TEXT || 'backpack', limit: 5 };
  if (__ENV.SEARCH_CATEGORY) body.category = __ENV.SEARCH_CATEGORY;
  const response = http.post(`${__ENV.MATCHING_API_URL.replace(/\/$/, '')}/v1/search`, JSON.stringify(body), {
    headers: { Authorization: `Bearer ${__ENV.LOSTLINK_ACCESS_TOKEN}`, 'Content-Type': 'application/json' },
    tags: { name: 'text-search-smoke' }, timeout: '15s', redirects: 0,
  });
  let result;
  try { result = response.json(); } catch { result = null; }
  check(response, {
    'search returns 200': r => r.status === 200,
    'response follows search contract': () => Array.isArray(result?.items) && result.items.length <= 5 && typeof result.model_version === 'string',
    'scores are cosine similarities': () => Array.isArray(result?.items) && result.items.every(item => typeof item.item_id === 'string' && Number.isFinite(item.score) && item.score >= -1 && item.score <= 1),
    'seeded item is found when specified': () => !__ENV.EXPECT_ITEM_ID || result?.items?.some(item => item.item_id === __ENV.EXPECT_ITEM_ID),
  });
  sleep(1);
}
