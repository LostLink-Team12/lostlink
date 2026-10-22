import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { reportHtml } from './capture-scaling.mjs';
export async function render({summaryPath,scalingPath,out}) {
  const summary=JSON.parse(await readFile(summaryPath,'utf8'));
  const evidence=scalingPath?JSON.parse(await readFile(scalingPath,'utf8')):null;
  const metric=(name,key)=>summary.metrics?.[name]?.values?.[key] ?? null;
  const thresholds=Object.values(summary.metrics || {}).flatMap(m=>Object.values(m.thresholds || {}));
  const passed=thresholds.length>0 && thresholds.every(t=>t.ok===true);
  const result={scenario:summary.scenario,run_id:summary.run_id,thresholds_passed:passed,
    http_requests:metric('http_reqs','count'),requests_per_second:metric('http_reqs','rate'),p95_ms:metric('http_req_duration','p(95)'),http_failure_rate:metric('http_req_failed','rate'),
    photos_confirmed:metric('photos_confirmed','count'),photos_reused:metric('photos_reused','count') ?? 0,
    scaling_evidence:evidence?{samples:evidence.samples.length,incomplete:evidence.samples.some(s=>s.errors?.length)}:null};
  const lines=['# LostLink load run','',...Object.entries(result).map(([k,v])=>`- ${k}: ${v===null?'unavailable':typeof v==='object'?JSON.stringify(v):v}`),'','HTTP p95 includes all HTTP operations for bulk import. Use the search run for search p95.','Photo confirmation is not embedding completion. Reused items did not create a fresh embedding backlog.','Replica changes alone do not prove HPA/KEDA caused scaling; inspect controller conditions and Grafana.'];
  if(!evidence || result.scaling_evidence.incomplete)lines.push('','Scaling acceptance: incomplete evidence. No successful dev scaling claim can be made.');
  await mkdir(out,{recursive:true});
  await writeFile(resolve(out,'report.md'),lines.join('\n')+'\n');
  await writeFile(resolve(out,'key-numbers.json'),JSON.stringify(result,null,2)+'\n');
  const html=reportHtml(evidence?.samples || [],{...result,source_summary:summaryPath,source_scaling:scalingPath || 'not supplied'});
  await writeFile(resolve(out,'report.html'),html);
  return result;
}
if(process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href) {
  const args=process.argv.slice(2),opt=(key,fallback)=>args.includes(key)?args[args.indexOf(key)+1]:fallback;
  if(!opt('--summary'))throw Error('Usage: node render-results.mjs --summary FILE [--scaling FILE] --out DIRECTORY');
  console.log(JSON.stringify(await render({summaryPath:resolve(opt('--summary')),scalingPath:opt('--scaling')?resolve(opt('--scaling')):undefined,out:resolve(opt('--out','tests/load/results/report'))}),null,2));
}
