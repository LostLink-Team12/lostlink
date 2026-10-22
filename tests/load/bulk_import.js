import http from 'k6/http';
import { multipartPhoto } from './multipart.js';
import { sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate } from 'k6/metrics';
import { SharedArray } from 'k6/data';
import { sha256 } from 'k6/crypto';
import { open as fsOpen, SeekMode } from 'k6/experimental/fs';
import { apiBase, validRun, integer, importBatch, summary } from './workload.js';
const base=apiBase(__ENV.ITEM_API_URL), runId=validRun(__ENV.RUN_ID);
const rows=new SharedArray('import-manifest',()=>JSON.parse(open(__ENV.LOAD_MANIFEST || './generated/manifest.json')));
const photos=await fsOpen(__ENV.LOAD_PHOTOS || './generated/photos.bin');
const count=integer(__ENV.IMPORT_COUNT,5000,1,5000),batchSize=integer(__ENV.BATCH_SIZE,50,1,100);
if (rows.length<count) throw Error('Prepared manifest has fewer rows than IMPORT_COUNT');
const confirmed=new Counter('photos_confirmed'),reused=new Counter('photos_reused'),batches=new Rate('batches_succeeded');
export const options={
  scenarios:{bulk:{executor:'shared-iterations',vus:integer(__ENV.IMPORT_VUS,10,1,50),iterations:Math.ceil(count/batchSize),maxDuration:__ENV.IMPORT_DURATION || '45m',gracefulStop:'1m'}},
  thresholds:{photos_confirmed:[`count==${count}`],batches_succeeded:['rate==1'],http_req_failed:['rate<0.01']},
  systemTags:['status','method','name','scenario','expected_response'],summaryTrendStats:['avg','min','med','max','p(95)','p(99)'],
};
const pollTimeout=integer(__ENV.POLL_TIMEOUT_SECONDS,600,1,3600)*1000;
async function readPhoto(row) {
  await photos.seek(row.offset,SeekMode.Start);
  const bytes=new Uint8Array(row.size); let read=0;
  while(read<row.size) { const n=await photos.read(bytes.subarray(read)); if (!n) throw Error('Truncated photo bundle'); read+=n; }
  if (sha256(bytes.buffer,'hex')!==row.sha256) throw Error('Photo checksum mismatch');
  return bytes.buffer;
}
async function api(path, {body,key,version}={}) {
  const headers={Authorization:'Bearer '+__ENV.LOSTLINK_ACCESS_TOKEN};
  if (body!==undefined) headers['Content-Type']='application/json';
  if (key) headers['Idempotency-Key']=key;
  if (version!==undefined) headers['If-Match']='"'+version+'"';
  const name=path.startsWith('/v1/item-operations/')?'operation':path.includes('/confirm')?'confirm':path.endsWith('/uploads')?'upload-ticket':path.startsWith('/v1/item-imports/')?'import-status':path==='/v1/item-imports'?'import-batch':'item-detail';
  for (let attempt=0;attempt<3;attempt++) {
    const res=http.request(body===undefined?'GET':'POST',base+path,body===undefined?null:JSON.stringify(body),{headers,tags:{name},timeout:'30s',redirects:0});
    if ([401,403].includes(res.status)) exec.test.abort('Staff access denied; configure a valid unexpired staff token.');
    if (res.status>=200 && res.status<300) { try { return res.json(); } catch { throw Error('Non-JSON API response for '+name); } }
    if (!(res.status===0 || res.status===429 || res.status>=500) || attempt===2) throw Error('API '+name+' failed (HTTP '+res.status+')');
    sleep(attempt+1);
  }
}
async function upload(target,photo) {
  apiBase(target.url.split('?')[0]);
  const form=multipartPhoto(target.fields,photo,'lostlink-'+sha256(`${Date.now()}:${__VU}:${__ITER}:${Math.random()}`,'hex').slice(0,40));
  for(let attempt=0;attempt<3;attempt++) {
    const res=http.post(target.url,form.body,{headers:{'Content-Type':form.contentType},tags:{name:'s3-upload'},timeout:'60s',redirects:0});
    if ([200,201,204].includes(res.status)) return;
    if (!(res.status===0 || res.status===429 || res.status>=500) || attempt===2) throw Error('S3 upload failed (HTTP '+res.status+')');
    sleep(attempt+1);
  }
}
export async function setup() {
  if (!__ENV.LOSTLINK_ACCESS_TOKEN) throw Error('Set LOSTLINK_ACCESS_TOKEN to a fresh venue staff token');
  // Validate the entire bundle before the first mutation; memory stays bounded to one photo.
  for (let i=0;i<count;i++) await readPhoto(rows[i]);
  await api('/v1/items?limit=1');
}
export default async function() {
  const start=exec.scenario.iterationInTest*batchSize, selected=[];
  for(let i=start;i<Math.min(start+batchSize,count);i++) selected.push(rows[i]);
  try {
    await importBatch(selected,{api,readPhoto,upload,pause:async()=>sleep(1),now:Date.now,hash:s=>sha256(s,'hex'),runId,timeoutMs:pollTimeout,onConfirmed:already=>{confirmed.add(1);if(already)reused.add(1);}});
    batches.add(true);
  } catch (e) { batches.add(false); throw e; }
}
export function handleSummary(data) {
  return {[__ENV.SUMMARY_PATH || 'bulk-summary.json']:summary(data,'bulk-import',runId),stdout:'Import run finished. photos_confirmed includes resumed items; this does not assert embedding availability or KEDA scaling.\n'};
}
