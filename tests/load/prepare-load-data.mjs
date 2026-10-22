import { readFile, writeFile, mkdir, open, rename } from 'node:fs/promises';
import { resolve, relative, isAbsolute, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
const here=dirname(fileURLToPath(import.meta.url));
export async function prepare(source=resolve(here,'../data/generated'),target=resolve(here,'generated'),count=5000) {
  const all=JSON.parse(await readFile(resolve(source,'manifest.json'),'utf8'));
  if (!Number.isInteger(count) || count<1 || count>all.length) throw Error('Requested item count exceeds source manifest');
  const rows=all.slice(0,count);
  if (new Set(rows.map(r=>r.client_reference)).size!==count) throw Error('Duplicate references');
  await mkdir(target,{recursive:true});
  const output=await open(resolve(target,'photos.bin.part'),'w');
  const packed=[]; let offset=0;
  try {
    for (const row of rows) {
      const photo=resolve(source,row.photo_path),within=relative(source,photo);
      if (within.startsWith('..') || isAbsolute(within)) throw Error('Photo escapes source directory');
      const bytes=await readFile(photo);
      if (!row.sha256 || createHash('sha256').update(bytes).digest('hex')!==row.sha256 || bytes[0]!==255 || bytes[1]!==216 || bytes.length>10*1024*1024) throw Error('Invalid photo/checksum: '+row.client_reference);
      let written=0; while (written<bytes.length) { const part=await output.write(bytes,written,bytes.length-written); if (!part.bytesWritten) throw Error('Short bundle write'); written+=part.bytesWritten; }
      packed.push({client_reference:row.client_reference,item:row.item,sha256:row.sha256,offset,size:bytes.length}); offset+=bytes.length;
    }
  } finally { await output.close(); }
  await rename(resolve(target,'photos.bin.part'),resolve(target,'photos.bin'));
  await writeFile(resolve(target,'manifest.json'),JSON.stringify(packed)+'\n');
  return {items:packed.length,bytes:offset};
}
if (process.argv[1] && resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  const args=process.argv.slice(2),option=(key,fallback)=>args.includes(key)?args[args.indexOf(key)+1]:fallback;
  console.log(JSON.stringify(await prepare(resolve(option('--source',resolve(here,'../data/generated'))),resolve(option('--out',resolve(here,'generated'))),Number(option('--count','5000')))));
}
