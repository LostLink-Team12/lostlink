import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { readFile,writeFile,mkdtemp,mkdir } from 'node:fs/promises';
import { join,resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import { prepare } from './prepare-load-data.mjs';
const k6=process.env.K6_BIN;
function run(script,env) {
  return new Promise((done,reject)=> {
    const child=spawn(k6,['run','--quiet','--no-color',script],{env:{...process.env,K6_NO_USAGE_REPORT:'true',...env},windowsHide:true});
    let output='';child.stdout.on('data',b=>output+=b);child.stderr.on('data',b=>output+=b);child.on('error',reject);
    const timeout=setTimeout(()=>child.kill(),60000);
    child.on('close',code=>{clearTimeout(timeout);done({code,output});});
  });
}
test('real k6 executes search ramps, multipart uploads, poll, resume and failure thresholds against a local contract double', {skip:!k6,timeout:120000},async()=> {
  let imports=0,uploads=0,confirmed=0,rejectSearch=false,uploadLeak=false,uploadHeaders=[],versionHeaders=[],keys=[],pendingReads=0;
  const jobs=new Map(),items=new Map();
  const server=createServer(async(req,res)=> {
    const buffers=[];for await(const b of req)buffers.push(b);const raw=Buffer.concat(buffers);
    const send=(status,body)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(body));};
    if(req.url==='/v1/search')return send(rejectSearch?503:200,{items:[{item_id:'bag',score:0.6}],model_version:'clip-mock'});
    if(req.url==='/v1/items?limit=1')return send(200,{items:[]});
    if(req.url==='/v1/item-imports') {
      const data=JSON.parse(raw);const job={id:data.batch_reference,entries:data.entries};jobs.set(job.id,job);imports++;
      for(const e of job.entries)if(!items.has(e.client_reference))items.set(e.client_reference,{state:'awaiting_upload',version:4});
      keys.push(req.headers['idempotency-key']);return send(202,{id:job.id});
    }
    if(req.url.startsWith('/v1/item-imports/')) {
      const id=decodeURIComponent(req.url.split('/')[3].split('?')[0]),job=jobs.get(id);
      return send(200,{job:{total:job.entries.length,accepted:job.entries.length,failed:0},items:job.entries.map(e=>({state:'succeeded',client_reference:e.client_reference,item_id:e.client_reference})),has_more:false});
    }
    if(req.url.startsWith('/v1/items/')) {
      const id=decodeURIComponent(req.url.split('/')[3]);
      if(req.url.endsWith('/uploads')) {versionHeaders.push(req.headers['if-match']);return send(201,{upload_id:'u-'+id,upload:{url:base+'/s3',fields:{key:'staging/'+id,policy:'signed-policy'}}});}
      if(req.url.endsWith('/confirm')){items.get(id).state='processing';return send(202,{operation_id:'op-'+id});}
      return send(200,items.get(id));
    }
    if(req.url==='/s3') {
      if(req.headers.authorization)uploadLeak=true;
      const text=raw.toString('latin1');assert(text.indexOf('name="policy"')<text.indexOf('name="file"'));
      uploadHeaders.push(req.headers['content-type']);uploads++;res.writeHead(204);return res.end();
    }
    if(req.url.startsWith('/v1/item-operations/')) {pendingReads++;if(pendingReads===1)return send(200,{state:'pending'});confirmed++;return send(200,{state:'completed'});}
    send(404,{});
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));const base='http://127.0.0.1:'+server.address().port;
  const root=await mkdtemp(join(tmpdir(),'lostlink-k6-')),source=join(root,'source');await mkdir(source);
  const bytes=Buffer.from([255,216,255,217]);await writeFile(join(source,'a.jpg'),bytes);
  const sha256=createHash('sha256').update(bytes).digest('hex');
  await writeFile(join(source,'manifest.json'),JSON.stringify(Array.from({length:4},(_,i)=>({client_reference:'demo-'+i,photo_path:'a.jpg',sha256,item:{title:'Bag',description:'Demo bag',category:'Backpack',found_at:'2026-10-17T04:00:00Z',verification_questions:[{prompt:'Receipt?',reference_answer:'demo'}]}}))));
  await prepare(source,join(root,'packed'),4);
  try {
    const common={LOSTLINK_ACCESS_TOKEN:'local-contract-double',RUN_ID:'local-test'};
    let result=await run('tests/load/search_load.js',{...common,MATCHING_API_URL:base,MAX_VUS:'2',RAMP_DURATION:'1s',HOLD_DURATION:'1s',THINK_SECONDS:'0',SUMMARY_PATH:join(root,'search.json')});
    assert.equal(result.code,0,result.output);
    const search=JSON.parse(await readFile(join(root,'search.json'),'utf8'));assert(search.metrics.http_reqs.values.count>0);assert(search.metrics.http_req_duration.values['p(95)']>=0);
    const bulk={...common,ITEM_API_URL:base,IMPORT_COUNT:'4',BATCH_SIZE:'2',IMPORT_VUS:'2',IMPORT_DURATION:'30s',LOAD_MANIFEST:join(root,'packed/manifest.json'),LOAD_PHOTOS:join(root,'packed/photos.bin'),SUMMARY_PATH:join(root,'bulk.json')};
    result=await run('tests/load/bulk_import.js',bulk);assert.equal(result.code,0,result.output);assert.equal(uploads,4);assert.equal(confirmed,4);assert.equal(uploadLeak,false);assert(versionHeaders.every(x=>x==='"4"'));assert(uploadHeaders.every(x=>x.startsWith('multipart/form-data; boundary=')));
    const initialKeys=[...keys].sort();const metrics=JSON.parse(await readFile(join(root,'bulk.json'),'utf8')).metrics;assert.equal(metrics.photos_confirmed.values.count,4);
    result=await run('tests/load/bulk_import.js',bulk);assert.equal(result.code,0,result.output);assert.equal(uploads,4);assert.deepEqual(keys.slice(2).sort(),initialKeys);
    assert.equal(JSON.parse(await readFile(join(root,'bulk.json'),'utf8')).metrics.photos_reused.values.count,4);
    rejectSearch=true;result=await run('tests/load/search_load.js',{...common,MATCHING_API_URL:base,MAX_VUS:'1',RAMP_DURATION:'1s',HOLD_DURATION:'1s',SUMMARY_PATH:join(root,'failed.json')});assert.notEqual(result.code,0);
    assert(!result.output.includes(common.LOSTLINK_ACCESS_TOKEN));
  } finally { server.closeAllConnections(); await new Promise(r=>server.close(r)); }
});
