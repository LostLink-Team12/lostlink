import http from 'k6/http';
import { check, sleep } from 'k6';
import exec from 'k6/execution';
import { Rate } from 'k6/metrics';
import { apiBase, integer, searchStages, searchBody, searchResult, summary } from './workload.js';
const base=apiBase(__ENV.MATCHING_API_URL), valid=new Rate('valid_searches');
const p95=integer(__ENV.P95_MS,5000), runId=__ENV.RUN_ID || 'search';
export const options={
  scenarios:{search:{executor:'ramping-vus',startVUs:0,stages:searchStages(__ENV),gracefulRampDown:'30s'}},
  thresholds:{http_req_failed:['rate<0.01'],http_req_duration:[`p(95)<${p95}`],valid_searches:['rate==1']},
  systemTags:['status','method','name','scenario','expected_response'],
  summaryTrendStats:['avg','min','med','max','p(90)','p(95)','p(99)'],
};
function query() {
  const res=http.post(base+'/v1/search',JSON.stringify(searchBody(__ENV)),{headers:{Authorization:'Bearer '+__ENV.LOSTLINK_ACCESS_TOKEN,'Content-Type':'application/json'},tags:{name:'search'},timeout:'30s',redirects:0});
  if ([401,403].includes(res.status)) exec.test.abort('Owner access denied; configure a valid unexpired owner access token.');
  let body; try { body=res.json(); } catch { body=null; }
  const ok=check(res,{'search contract and expected item':r=>r.status===200 && searchResult(body,__ENV.EXPECT_ITEM_ID)});
  valid.add(ok);
  return ok;
}
export function setup() {
  if (!__ENV.LOSTLINK_ACCESS_TOKEN) throw Error('Set LOSTLINK_ACCESS_TOKEN to a fresh owner access token');
  if (!query()) throw Error('Search preflight failed');
}
export default function() { query(); sleep(integer(__ENV.THINK_SECONDS,1,0,60)); }
export function handleSummary(data) {
  return {[__ENV.SUMMARY_PATH || 'search-summary.json']:summary(data,'search',runId),stdout:'Search run finished; see summary JSON for p95, throughput and threshold results.\n'};
}
