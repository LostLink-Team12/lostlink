import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join,resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { integer, apiBase, validRun, searchStages, searchResult, importBatch } from './workload.js';
import { prepare } from './prepare-load-data.mjs';
import { capture, metricValues, reportHtml } from './capture-scaling.mjs';
const hash=s=>createHash('sha256').update(s).digest('hex');

test('load configuration rejects unsafe URLs, malformed runs and preserves the 200 VU ramp',()=> {
  assert.throws(()=>apiBase('http://remote.test'));assert.throws(()=>apiBase('https://secret@remote.test'));assert.throws(()=>apiBase('https://remote.test?x=y'));assert.throws(()=>validRun(''));
  assert.throws(()=>integer('NaN',1));assert.equal(apiBase('http://127.0.0.1:8080/'),'http://127.0.0.1:8080');
  assert.deepEqual(searchStages({}).map(s=>s.target),[25,50,100,200,0]);
  assert.equal(searchResult({items:[{item_id:'x',score:0.6}],model_version:'clip'},'x'),true);
  assert.equal(searchResult({items:[{item_id:'x',score:NaN}],model_version:'clip'}),false);
});

function harness({state='awaiting_upload',failed=false,pending=false}={}) {
  const calls=[],counts=[],row={client_reference:'photo1',item:{title:'Bag'},sha256:'checksum'};let time=0;
  const deps={hash,runId:'demo',timeoutMs:2,now:()=>time,pause:async()=>{time++},readPhoto:async()=>new ArrayBuffer(4),upload:async(target)=>calls.push({upload:target}),onConfirmed:already=>counts.push(already),api:async(path,options)=> {
    calls.push({path,options});
    if(path==='/v1/item-imports')return {id:'job'};
    if(path.includes('/item-imports/job'))return {job:{accepted:pending?0:1,total:1,failed:0},items:[{state:'succeeded',client_reference:'demo-'+hash('photo1').slice(0,32),item_id:'i1'}]};
    if(path==='/v1/items/i1')return {state,version:4};
    if(path.endsWith('/uploads'))return {upload_id:'u1',upload:{url:'https://s3.test'}};
    if(path.endsWith('/confirm'))return {operation_id:'op1'};
    if(path.includes('/item-operations/'))return {state:failed?'needs_attention':'completed'};
    throw Error('Unexpected path');
  }};
  return {row,deps,calls,counts};
}
test('bulk import uses deterministic names, correct version and only counts completed operations',async()=> {
  const a=harness();await importBatch([a.row],a.deps);assert.deepEqual(a.counts,[false]);
  assert.equal(a.calls.find(c=>c.path?.endsWith('/uploads')).options.version,4);
  const b=harness();await importBatch([b.row],b.deps);assert.equal(a.calls[0].options.key,b.calls[0].options.key);
  assert.equal(a.calls.filter(c=>c.upload).length,1);
});
test('resumed processing items do not get uploaded again',async()=> {
  const h=harness({state:'processing'});await importBatch([h.row],h.deps);assert.deepEqual(h.counts,[true]);assert.equal(h.calls.some(c=>c.upload),false);
});
test('terminal failures and polling timeouts cannot be counted as confirmation',async()=> {
  for(const settings of [{failed:true},{pending:true}]) {const h=harness(settings);await assert.rejects(importBatch([h.row],h.deps));assert.equal(h.counts.length,0);}
});
test('preparation rejects corrupt or escaping files before a workload can use them',async()=> {
  const root=await mkdtemp(join(tmpdir(),'lostlink-pack-')); const source=join(root,'source');await mkdir(source); const bytes=Buffer.from([255,216,255,217]);await writeFile(join(source,'a.jpg'),bytes);
  const row={client_reference:'one',item:{title:'Bag'},photo_path:'a.jpg',sha256:hash(bytes)};
  await writeFile(join(source,'manifest.json'),JSON.stringify([row]));
  assert.deepEqual(await prepare(source,join(root,'out'),1),{items:1,bytes:4});
  assert.deepEqual(await readFile(join(root,'out/photos.bin')),bytes);
  await writeFile(join(source,'a.jpg'),'broken');await assert.rejects(prepare(source,join(root,'out'),1),/checksum/);
  await writeFile(join(source,'manifest.json'),JSON.stringify([{...row,photo_path:'../a.jpg'}]));await assert.rejects(prepare(source,join(root,'out'),1),/escapes/);
});
test('evidence records missing metrics as unavailable and fails incomplete capture',async()=> {
  const out=await mkdtemp(join(tmpdir(),'lostlink-evidence-'));
  const result=await capture({context:'test',namespace:'lostlink',selector:'app=matching',prometheus:'http://127.0.0.1:9090',backlogQuery:'queue',out,seconds:0},{execute:async()=>({stdout:'{"items":[]}'}),fetch:async()=>({ok:true,json:async()=>({status:'success',data:{resultType:'vector',result:[]}})})});
  assert.equal(result.incomplete,true);
  const saved=JSON.parse(await readFile(join(out,'scaling.json'),'utf8'));assert.deepEqual(saved.samples[0].metrics.embedding_backlog,[]);
  assert.match(await readFile(join(out,'scaling.html'),'utf8'),/unavailable, never zero/);
  assert.deepEqual(metricValues({status:'success',data:{resultType:'vector',result:[{metric:{},value:[0,'NaN']}]}}),[]);
  assert(!reportHtml([],{context:'<script>'}).includes('<script>'));
});

test('valid capture preserves real series and report never claims missing thresholds passed',async()=> {
  const {render}=await import('./render-results.mjs');
  const root=await mkdtemp(join(tmpdir(),'lostlink-report-'));
  const vector={status:'success',data:{resultType:'vector',result:[{metric:{},value:[0,'3']}]}};
  const result=await capture({context:'test-only',namespace:'lostlink',selector:'app=test',prometheus:'http://127.0.0.1:9090',backlogQuery:'test_backlog',out:root,seconds:0},{execute:async(_cmd,args)=>({stdout:JSON.stringify({items:args.includes('deployments')?[{metadata:{name:'tasks'},spec:{replicas:3},status:{readyReplicas:2}}]:args.includes('hpa')?[{metadata:{name:'tasks-hpa'},spec:{scaleTargetRef:{name:'tasks'}},status:{currentReplicas:2,desiredReplicas:3}}]:[{metadata:{name:'tasks-scaled'},spec:{scaleTargetRef:{name:'tasks'},triggers:[{type:'postgresql'}]}}]})}),fetch:async()=>({ok:true,json:async()=>vector})});
  assert.equal(result.incomplete,false);
  const summaryPath=join(root,'summary.json');await writeFile(summaryPath,JSON.stringify({scenario:'unit-test-only',metrics:{http_reqs:{values:{count:4,rate:2}},photos_confirmed:{values:{count:4}}}}));
  const report=await render({summaryPath,scalingPath:join(root,'scaling.json'),out:join(root,'report')});
  assert.equal(report.http_requests,4);assert.equal(report.p95_ms,null);assert.equal(report.thresholds_passed,false);
  assert.match(await readFile(join(root,'report/report.html'),'utf8'),/tasks ready/);
});

test('bulk capture can stop gracefully without requiring a search latency series',async()=> {
  const out=await mkdtemp(join(tmpdir(),'lostlink-stop-'));
  const stopFile=join(out,'stop');await writeFile(stopFile,'stop');
  const queries=[];
  const result=await capture({context:'test',namespace:'lostlink',selector:'app=test',prometheus:'http://127.0.0.1:9090',backlogQuery:'queue',out,seconds:120,stopFile,includeSearchP95:false},{execute:async(_cmd,args)=>({stdout:JSON.stringify({items:args.includes('deployments')?[{metadata:{name:'tasks'},spec:{replicas:1},status:{readyReplicas:1}}]:args.includes('hpa')?[{metadata:{name:'tasks'},spec:{scaleTargetRef:{name:'tasks'}}}]:[{metadata:{name:'tasks'},spec:{scaleTargetRef:{name:'tasks'},triggers:[{type:'postgresql'}]}}]})}),fetch:async url=>{queries.push(url.searchParams.get('query'));return {ok:true,json:async()=>({status:'success',data:{resultType:'vector',result:[{metric:{},value:[0,'0']}]}})}},pause:async()=>{throw Error('Should stop before waiting')}});
  assert.equal(result.samples,1);assert.equal(result.incomplete,false);assert.deepEqual(queries,['queue']);
  assert.match(await readFile(join(out,'scaling.csv'),'utf8'),/embedding_backlog/);
  assert.doesNotMatch(await readFile(join(out,'scaling.csv'),'utf8'),/p95_seconds/);
});
