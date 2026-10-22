import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { writeFile, mkdir, access } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const execute=promisify(execFile);
export function summarizeKube(deployments,hpas,scaled) {
  return {
    deployments:deployments.items.map(d=>({name:d.metadata.name,desired:d.spec.replicas ?? 1,ready:d.status?.readyReplicas ?? 0,available:d.status?.availableReplicas ?? 0})),
    hpas:hpas.items.map(h=>({name:h.metadata.name,target:h.spec.scaleTargetRef.name,current:h.status?.currentReplicas ?? null,desired:h.status?.desiredReplicas ?? null,conditions:(h.status?.conditions || []).map(c=>({type:c.type,status:c.status}))})),
    scaledObjects:scaled.items.map(s=>({name:s.metadata.name,target:s.spec.scaleTargetRef.name,triggers:(s.spec.triggers || []).map(t=>t.type),conditions:(s.status?.conditions || []).map(c=>({type:c.type,status:c.status}))})),
  };
}
export function metricValues(response) {
  if (response.status!=='success' || response.data?.resultType!=='vector') throw Error('Invalid Prometheus vector');
  return response.data.result.map(r=>({labels:r.metric,value:Number(r.value[1])})).filter(r=>Number.isFinite(r.value));
}
export function reportHtml(samples,metadata) {
  const escape=x=>String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const series=new Map();
  for(const [i,s] of samples.entries()) {
    for(const d of s.kube?.deployments || []) for(const key of ['desired','ready']) {
      const name=d.name+' '+key; if(!series.has(name))series.set(name,[]); series.get(name).push([i,d[key]]);
    }
    for(const [key,values] of Object.entries(s.metrics || {})) if(values.length===1) {
      if(!series.has(key))series.set(key,[]); series.get(key).push([i,values[0].value]);
    }
  }
  const charts=[...series].map(([name,values])=>{
    const max=Math.max(1,...values.map(([,v])=>v)),points=values.map(([x,y],i)=>`${i && values[i-1][0]===x-1 ? 'L' : 'M'}${45+x*700/Math.max(1,samples.length-1)},${200-y/max*170}`).join(' ');
    return `<section><h2>${escape(name)}</h2><p>Observed range ${Math.min(...values.map(([,v])=>v))}–${Math.max(...values.map(([,v])=>v))}. Horizontal axis: sample index; vertical axis: 0–${max.toFixed(3)}. Missing samples are gaps in the source data.</p><svg viewBox="0 0 780 230" role="img" aria-label="${escape(name)}"><path d="M45 20V200H750" fill="none" stroke="#94a3b8"/><path d="${points}" fill="none" stroke="#2563eb" stroke-width="3"/></svg></section>`;
  }).join('');
  const errors=samples.flatMap((s,i)=>(s.errors || []).map(e=>`Sample ${i}: ${e}`));
  return `<!doctype html><meta charset="utf-8"><title>LostLink scaling evidence</title><style>body{font:16px system-ui;max-width:1050px;margin:40px auto;padding:20px;color:#172033;background:#f5f7fa}section{background:white;padding:20px;margin:20px 0;border:1px solid #ddd;border-radius:8px}svg{width:100%}pre{white-space:pre-wrap}.warning{color:#9a3412}</style><h1>LostLink scaling evidence</h1><p>Read-only observations; ${samples.length} samples. No scaling success is inferred automatically. Compare desired and ready replicas, backlog drain, cooldown and the k6 summary.</p><pre>${escape(JSON.stringify(metadata,null,2))}</pre><p class="warning">Collection errors: ${errors.length}. Empty/missing metrics are unavailable, never zero.</p><pre>${escape(errors.join('\n'))}</pre>${charts || '<p>No chartable observations. C3 prerequisites may be missing.</p>'}`;
}
export async function capture({context,namespace,selector,prometheus,backlogQuery,out,seconds=1200,interval=15,kubectl='kubectl',query,stopFile,includeSearchP95=true}, deps={}) {
  if (!context || !namespace || !selector || !backlogQuery || !prometheus) throw Error('Set --context, --namespace, --selector, --prometheus and --backlog-query from the deployed C3 configuration');
  if (!/^[a-z0-9-]+$/.test(namespace) || !Number.isInteger(seconds) || seconds<0 || !Number.isInteger(interval) || interval<1) throw Error('Invalid capture configuration');
  const url=new URL(prometheus); if(url.username||url.password||url.search||url.hash || (url.protocol!=='https:' && !(url.protocol==='http:' && ['127.0.0.1','localhost','[::1]'].includes(url.hostname)))) throw Error('Prometheus requires HTTPS or localhost without URL credentials');
  const run=deps.execute || execute, request=deps.fetch || fetch, pause=deps.pause || (ms=>new Promise(r=>setTimeout(r,ms))),clock=deps.now || Date.now;
  await mkdir(out,{recursive:true});
  const samples=[],start=clock(), metadata={context,namespace,selector,started_at:new Date(start).toISOString(),interval_seconds:interval,backlog_query:backlogQuery,search_p95_collected:includeSearchP95};
  const queries={...(includeSearchP95?{p95_seconds:query || `histogram_quantile(0.95, sum by (le) (rate(lostlink_http_request_duration_seconds_bucket{namespace="${namespace}",service="matching-service",route="/v1/search",method="POST"}[2m])))`}:{}),embedding_backlog:backlogQuery};
  do {
    const sample={time:new Date(clock()).toISOString(),metrics:{},errors:[]};
    const kube=await Promise.allSettled(['deployments','hpa','scaledobjects.keda.sh'].map(async resource=> {
      const result=await run(kubectl,['--context',context,'--namespace',namespace,'get',resource,'--selector',selector,'-o','json'],{timeout:30000,maxBuffer:4*1024*1024,windowsHide:true});return JSON.parse(result.stdout);
    }));
    if(kube.every(r=>r.status==='fulfilled')) {
      sample.kube=summarizeKube(...kube.map(r=>r.value));
      if(!sample.kube.deployments.length || !sample.kube.hpas.length || !sample.kube.scaledObjects.length) sample.errors.push('Selected C3 deployments/HPA/ScaledObject missing');
      if(!sample.kube.scaledObjects.some(s=>s.triggers.includes('postgresql'))) sample.errors.push('No selected PostgreSQL KEDA trigger');
    } else sample.errors.push('kubectl collection failed (check context, RBAC and KEDA CRDs)');
    for(const [key,promql] of Object.entries(queries)) {
      try {
        const endpoint=new URL(url.href.replace(/\/$/,'')+'/api/v1/query');endpoint.searchParams.set('query',promql);
        const response=await request(endpoint,{headers:process.env.PROMETHEUS_TOKEN?{Authorization:'Bearer '+process.env.PROMETHEUS_TOKEN}:{},signal:AbortSignal.timeout(15000),redirect:'error'});
        if(!response.ok)throw Error('HTTP '+response.status);
        const values=metricValues(await response.json()); sample.metrics[key]=values;
        if(values.length!==1) sample.errors.push(key+': expected one aggregate series, got '+values.length);
      } catch { sample.errors.push(key+': Prometheus query unavailable'); }
    }
    samples.push(sample);
    // Persist each sample so Ctrl+C still leaves usable JSON evidence.
    await writeFile(resolve(out,'scaling.json'),JSON.stringify({metadata,samples},null,2)+'\n');
    await writeFile(resolve(out,'scaling.html'),reportHtml(samples,metadata));
    if(stopFile) {
      try { await access(stopFile); break; }
      catch(error) { if(error.code!=='ENOENT') throw error; }
    }
    if(clock()-start>=seconds*1000)break;
    await pause(Math.min(interval*1000,seconds*1000-(clock()-start)));
  } while(true);
  const csv=['time,kind,name,value'];
  for(const s of samples) {
    const line=(kind,name,value)=>csv.push([s.time,kind,name,value].map(x=>'"'+String(x).replaceAll('"','""')+'"').join(','));
    for(const d of s.kube?.deployments || []) { line('desired_replicas',d.name,d.desired); line('ready_replicas',d.name,d.ready); }
    for(const [key,values] of Object.entries(s.metrics)) for(const value of values) line(key,JSON.stringify(value.labels),value.value);
  }
  await writeFile(resolve(out,'scaling.csv'),csv.join('\n')+'\n');
  return {samples:samples.length,incomplete:samples.some(s=>s.errors.length>0)};
}
if(process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href) {
  const args=process.argv.slice(2),opt=(key,fallback)=>args.includes(key)?args[args.indexOf(key)+1]:fallback;
  const result=await capture({context:opt('--context'),namespace:opt('--namespace'),selector:opt('--selector'),prometheus:opt('--prometheus'),backlogQuery:opt('--backlog-query'),out:resolve(opt('--out','tests/load/results')),seconds:Number(opt('--seconds','1200')),interval:Number(opt('--interval','15')),kubectl:opt('--kubectl','kubectl'),query:opt('--p95-query'),stopFile:opt('--stop-file'),includeSearchP95:!args.includes('--no-search-p95')});
  console.log(JSON.stringify(result));if(result.incomplete)process.exitCode=2;
}
