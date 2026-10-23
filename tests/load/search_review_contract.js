// Pure validation shared by k6 and Node's test runner. Never stringify input into errors.
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const fields = new Set([
  'text', 'photo_base64', 'category', 'venue_id', 'found_from', 'found_until', 'min_score', 'limit',
]);
function require(value, message) { if (!value) throw Error(message); }
function timestamp(value) {
  return typeof value === 'string' && /T.+(?:Z|[+-]\d{2}:\d{2})$/i.test(value) && Number.isFinite(Date.parse(value));
}

export function casesFrom(value) {
  require(value && Array.isArray(value.cases) && value.cases.length > 0 && value.cases.length <= 20, 'Provide 1–20 search cases');
  const names = new Set();
  return value.cases.map(entry => {
    require(entry && typeof entry.name === 'string' && /^[a-z][a-z0-9_-]{0,39}$/.test(entry.name) && !names.has(entry.name), 'Case names must be unique public labels');
    names.add(entry.name);
    const p = entry.payload;
    require(p && Object.keys(p).every(key => fields.has(key)), 'Search payload contains unsupported fields');
    require((typeof p.text === 'string' && p.text.trim().length > 0) || typeof p.photo_base64 === 'string', 'A text/photo query is required');
    require(p.text === undefined || (typeof p.text === 'string' && p.text.trim().length >= 1 && p.text.length <= 2000), 'Invalid query text');
    require(p.photo_base64 === undefined || (typeof p.photo_base64 === 'string' && p.photo_base64.length > 0 && p.photo_base64.length <= 6990508 && /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(p.photo_base64)), 'Provide raw base64 photo content');
    require(Number.isInteger(p.limit) && p.limit >= 1 && p.limit <= 100, 'Set an explicit result limit from 1 to 100');
    require(p.min_score === undefined || (Number.isFinite(p.min_score) && p.min_score >= -1 && p.min_score <= 1), 'Invalid cosine threshold');
    require(p.venue_id === undefined || uuid.test(p.venue_id), 'Invalid venue filter');
    require(p.category === undefined || (typeof p.category === 'string' && p.category.length > 0 && p.category.length <= 100), 'Invalid category filter');
    for (const key of ['found_from', 'found_until']) require(p[key] === undefined || timestamp(p[key]), 'Invalid time filter');
    require(!p.found_from || !p.found_until || Date.parse(p.found_from) <= Date.parse(p.found_until), 'Reversed time filter');
    require(Array.isArray(entry.expected_item_ids) && entry.expected_item_ids.length >= 1 && entry.expected_item_ids.length <= p.limit && entry.expected_item_ids.every(id => uuid.test(id)), 'Each case needs known expected item IDs');
    require(new Set(entry.expected_item_ids).size === entry.expected_item_ids.length, 'Expected item IDs must be distinct');
    const forbidden = entry.forbidden_item_ids || [];
    require(Array.isArray(forbidden) && forbidden.every(id => uuid.test(id)) && !entry.expected_item_ids.some(id => forbidden.includes(id)), 'Invalid forbidden item IDs');
    const min = entry.min_results ?? 1;
    require(Number.isInteger(min) && min >= 1 && min <= p.limit && min >= entry.expected_item_ids.length, 'Invalid minimum result count');
    return { ...entry, min_results: min, forbidden_item_ids: forbidden, mode: p.text ? (p.photo_base64 ? 'fused' : 'text') : 'photo' };
  });
}

export function responseValid(body, entry, candidateLimit, modelVersion) {
  if (!body || !Array.isArray(body.items)
    || body.items.length < entry.min_results || body.items.length > entry.payload.limit
    || body.limit !== entry.payload.limit || body.candidate_limit !== candidateLimit
    || typeof body.model_version !== 'string' || body.model_version.length === 0
    || typeof body.potentially_truncated !== 'boolean'
    || (modelVersion && body.model_version !== modelVersion)) return false;
  if (body.items.some(item => !item || typeof item !== 'object')) return false;
  const ids = body.items.map(item => item.item_id);
  if (new Set(ids).size !== ids.length
    || !entry.expected_item_ids.every(id => ids.includes(id))
    || entry.forbidden_item_ids.some(id => ids.includes(id))) return false;
  return body.items.every(item => {
    const p = entry.payload;
    return uuid.test(item.item_id) && uuid.test(item.tenant_id)
      && Number.isInteger(item.item_version) && item.item_version >= 1
      && Number.isFinite(item.score) && item.score >= -1 && item.score <= 1
      && item.score >= (p.min_score ?? 0)
      && typeof item.title === 'string' && item.title.length > 0
      && typeof item.description === 'string' && typeof item.category === 'string'
      && timestamp(item.found_at)
      && (item.photo_url === null || typeof item.photo_url === 'string')
      && (item.public_location_hint === null || typeof item.public_location_hint === 'string')
      && (!p.category || item.category === p.category)
      && (!p.venue_id || item.tenant_id === p.venue_id)
      && (!p.found_from || Date.parse(item.found_at) >= Date.parse(p.found_from))
      && (!p.found_until || Date.parse(item.found_at) <= Date.parse(p.found_until));
  });
}

export function configuration(env) {
  function integer(name, fallback, min, max) {
    const raw = env[name] ?? String(fallback);
    require(/^\d+$/.test(raw), 'Invalid numeric run option');
    const number = Number(raw);
    require(number >= min && number <= max, 'Numeric run option outside bounds');
    return number;
  }
  const profile = env.SEARCH_PROFILE || 'measurement';
  require(['measurement', 'validation'].includes(profile), 'Unknown search profile');
  const duration = env.SEARCH_DURATION || (profile === 'measurement' ? '60s' : '5s');
  const durationMatch = /^(\d+)(s|m)$/.exec(duration);
  const seconds = durationMatch && Number(durationMatch[1]) * (durationMatch[2] === 'm' ? 60 : 1);
  require(seconds >= (profile === 'measurement' ? 10 : 1) && seconds <= 3600, 'Invalid run duration');
  const baseUrl = env.MATCHING_API_URL || '';
  const scope = env.SEARCH_SCOPE || 'dev';
  require(['dev', 'local-fixture'].includes(scope), 'Unknown search scope');
  require(/^https:\/\/[^/?#@\s]+(?:\/[^?#@\s]*)?$/.test(baseUrl) || (scope === 'local-fixture' && /^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/.test(baseUrl)), 'Use fixed HTTPS or an explicit loopback fixture');
  require(typeof env.LOSTLINK_ACCESS_TOKEN === 'string' && env.LOSTLINK_ACCESS_TOKEN.length > 0, 'Set an owner access token through the environment');
  return {
    profile, scope, duration, vus: integer('SEARCH_VUS', 1, 1, 20),
    minSamples: integer('SEARCH_MIN_SAMPLES', profile === 'measurement' ? 100 : 5, profile === 'measurement' ? 100 : 1, 100000),
    warmup: integer('SEARCH_WARMUP', 5, 1, 100),
    candidateLimit: integer('EXPECTED_CANDIDATE_LIMIT', 100, 20, 100),
    baseUrl: baseUrl.replace(/\/$/, ''), modelVersion: env.EXPECTED_MODEL_VERSION || null,
  };
}

export function aggregateReport(data, cases, config) {
  function values(metric, name) { return data.metrics[`${metric}{case:${name}}`]?.values || {}; }
  const results = cases.map(entry => {
    const count = values('search_review_requests', entry.name).count || 0;
    const validity = values('search_review_valid', entry.name).rate ?? null;
    const latency = values('search_review_duration_ms', entry.name);
    const resultCounts = values('search_review_results', entry.name);
    const wall = values('search_review_wall_ms', entry.name);
    const enough = count >= config.minSamples;
    const p95 = count ? latency['p(95)'] ?? null : null;
    return {
      name: entry.name, mode: entry.mode, samples: count, valid_rate: validity,
      p95_ms: p95, p99_ms: count ? latency['p(99)'] ?? null : null,
      wall_p95_ms: count ? wall['p(95)'] ?? null : null,
      min_results: resultCounts.min ?? null, max_results: resultCounts.max ?? null,
      decision: !enough ? 'insufficient-samples' : validity !== 1 ? 'invalid-responses' : p95 === null || p95 > 500 ? 'investigate-latency' : 'within-500ms-for-this-run',
    };
  });
  const passed = results.every(result => result.decision === 'within-500ms-for-this-run');
  return {
    schema_version: 1, recorded_at_utc: new Date().toISOString(), scope: config.scope, profile: config.profile,
    vus: config.vus, duration: config.duration, warmup_per_case: config.warmup,
    required_samples_per_case: config.minSamples, candidate_limit: config.candidateLimit,
    criterion_ms: 500, acceptance_eligible: config.profile === 'measurement' && config.scope === 'dev' && passed,
    passed, cases: results,
  };
}
