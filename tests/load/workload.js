export function integer(value, fallback, min = 1, max = 100000) {
  const n = value === undefined || value === '' ? fallback : Number(value);
  if (!Number.isInteger(n) || n < min || n > max) throw Error('Invalid numeric load setting');
  return n;
}
export function apiBase(value) {
  if (typeof value !== 'string' || !/^https?:\/\/[^/?#]+(?:\/[^?#]*)?$/.test(value) || /https?:\/\/[^/]*@/.test(value)) throw Error('Set a fixed API URL without credentials, query or fragment');
  if (!value.startsWith('https://') && !/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:\/|$)/.test(value)) throw Error('API requires HTTPS or localhost');
  return value.replace(/\/$/,'');
}
export function validRun(value) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$/.test(value || '')) throw Error('Set RUN_ID to 1-40 letters, digits, _ or -; reuse it only to resume the same import');
  return value;
}
export function searchStages(env) {
  const max=integer(env.MAX_VUS,200,1,2000), ramp=env.RAMP_DURATION || '2m', hold=env.HOLD_DURATION || '3m';
  if (![ramp,hold].every(x=>/^\d+(ms|s|m|h)$/.test(x))) throw Error('Invalid stage duration');
  return [{duration:ramp,target:Math.max(1,Math.ceil(max/8))},{duration:hold,target:Math.max(1,Math.ceil(max/4))},{duration:hold,target:Math.max(1,Math.ceil(max/2))},{duration:hold,target:max},{duration:ramp,target:0}];
}
export function searchBody(env) {
  return { text: env.SEARCH_TEXT || 'backpack', limit:5, ...(env.SEARCH_CATEGORY ? {category:env.SEARCH_CATEGORY} : {}) };
}
export function searchResult(value, expected) {
  return Array.isArray(value?.items) && value.items.length <= 5 && typeof value.model_version === 'string' && value.items.every(x=>typeof x.item_id === 'string' && Number.isFinite(x.score) && Math.abs(x.score)<=1) && (!expected || value.items.some(x=>x.item_id===expected));
}
export function summary(data, scenario, runId) {
  const metrics = {};
  for (const [name,metric] of Object.entries(data.metrics || {})) metrics[name] = { values:metric.values, thresholds:metric.thresholds };
  return JSON.stringify({ scenario, run_id:runId, generated_at:new Date().toISOString(), metrics },null,2);
}

// Dependency injection keeps the actual import sequence testable outside k6.
export async function importBatch(rows, { api, readPhoto, upload, pause, now, hash, runId, timeoutMs, onConfirmed=()=>{} }) {
  const entries=rows.map(row=>({...row.item,client_reference:runId+'-'+hash(row.client_reference).slice(0,32)}));
  const batch=hash(JSON.stringify(entries)), key='load-'+batch;
  const job=await api('/v1/item-imports',{body:{batch_reference:key,entries},key});
  const poll=async (path, inspect) => {
    const deadline=now()+timeoutMs;
    while (true) {
      const value=await api(path), result=inspect(value);
      if (result) return value;
      if (now()>=deadline) throw Error('Operation polling timed out');
      await pause();
    }
  };
  const status=await poll('/v1/item-imports/'+encodeURIComponent(job.id)+'?limit=100',s=> {
    if (s.job.failed) throw Error('Import has failed entries');
    return s.job.accepted===s.job.total;
  });
  if (status.has_more || status.items.length!==rows.length) throw Error('Incomplete import result');
  const indexed=new Map(entries.map((entry,i)=>[entry.client_reference,rows[i]])), seen=new Set();
  for (const entry of status.items) {
    const row=indexed.get(entry.client_reference);
    if (!row || seen.has(entry.client_reference) || entry.state!=='succeeded' || !entry.item_id) throw Error('Invalid import entry');
    seen.add(entry.client_reference);
    const path='/v1/items/'+encodeURIComponent(entry.item_id), item=await api(path);
    if (['processing','available','reserved','handed_over'].includes(item.state)) { onConfirmed(true); continue; }
    if (item.state!=='awaiting_upload') throw Error('Unexpected item state: '+item.state);
    const photo=await readPhoto(row);
    const uploadKey='upload-'+hash(entry.item_id+':'+row.sha256+':'+item.version);
    const target=await api(path+'/uploads',{body:{content_type:'image/jpeg',size:photo.byteLength},version:item.version,key:uploadKey});
    await upload(target.upload,photo);
    const accepted=await api(path+'/uploads/'+encodeURIComponent(target.upload_id)+'/confirm',{body:{},key:'confirm-'+target.upload_id});
    await poll('/v1/item-operations/'+encodeURIComponent(accepted.operation_id),op=> {
      if (['failed','superseded','needs_attention'].includes(op.state)) throw Error('Photo operation: '+op.state);
      return op.state==='completed';
    });
    onConfirmed(false);
  }
}
