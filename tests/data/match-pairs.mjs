import { readFile, writeFile, mkdir, copyFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { parse } from 'csv-parse/sync';

const root = dirname(fileURLToPath(import.meta.url));
// Visually reviewed D1 context images. Descriptions deliberately avoid source titles/IDs.
const selections = [
  ['14d65997600bb7cd','0040009ad56c2bc2','I lost a red and black backpack with a dark outer pocket.'],
  ['0040009ad56c2bc2','04bed9439a2fb06f','My handbag is red with a floral pattern and two short handles.'],
  ['04bed9439a2fb06f','0040009ad56c2bc2','I am looking for a black handbag with orange and yellow flames along the bottom.'],
  ['002b0dc60b1068a9','06e90333a82a8361','I lost a small blue hard-sided case with a top handle and metal clasps.'],
  ['06e90333a82a8361','002b0dc60b1068a9','My old travel trunk has a flat green lid and worn brown edges.'],
  ['12b999f7e3b75de1','175aa1739edb9890','I lost a pale translucent umbrella with blue sections.'],
  ['175aa1739edb9890','12b999f7e3b75de1','My umbrella has a bright yellow canopy and dark spokes.'],
  ['012dc31b561d4214','043f3655deab6e7c','I lost a black rectangular touchscreen phone with several physical buttons below its screen.'],
  ['043f3655deab6e7c','012dc31b561d4214','My phone has a black front, a light metallic rim and a round home button.'],
  ['0421a6e230527185','09ea349ee555b61d','I lost a dark laptop with a black keyboard and Windows installed.'],
  ['09ea349ee555b61d','0421a6e230527185','I am looking for a silver laptop with a black keyboard and a camera above the screen.'],
  ['0908dd33cf950266','1eeda2f2e8f31933','My headphones have large red ear cups and a black headband.'],
  ['01284dd8eaad0569','022881fd81a5a8f7','I lost a black camera attached to a long white telephoto lens.'],
  ['022881fd81a5a8f7','01284dd8eaad0569','My vintage black camera has two round lenses stacked vertically.'],
  ['182c1f2bfa4b4280','02deba0102b5ce2a','I lost a white ceramic mug with a curved handle.'],
  ['128f92487d754a0c','398328305b2ed3d7','My black computer mouse has silver side buttons and a white logo.'],
  ['1735d6aa0d62cb00','1f5cf6e96f0e0690','I lost an off-white computer keyboard with grey keys.'],
];
const header = ['query_text','query_photo','item_description','item_photo','is_match'];
const quote = value => '"' + String(value).replaceAll('"','""') + '"';
const sha = bytes => createHash('sha256').update(bytes).digest('hex');

export async function build() {
  const manifest = JSON.parse(await readFile(resolve(root,'generated/manifest.json'),'utf8'));
  const source = new Map(manifest.map(row => [row.client_reference.replace('oi-validation-',''),row]));
  const folder = resolve(root,'match_photos'); await mkdir(folder,{recursive:true});
  const photos = [];
  for (const imageId of new Set(selections.flatMap(([positive,negative]) => [positive,negative]))) {
    const row = source.get(imageId);
    if (!row?.sha256) throw Error('Missing checksummed D1 image: ' + imageId);
    const path = resolve(root,'generated',row.photo_path), bytes = await readFile(path);
    if (sha(bytes) !== row.sha256) throw Error('D1 checksum mismatch: ' + imageId);
    if (bytes.length > 5 * 1024 * 1024) throw Error('Query photo exceeds 5 MiB: ' + imageId);
    await copyFile(path,resolve(folder,imageId + '.jpg'));
    photos.push({ image_id:imageId, photo_path:'match_photos/' + imageId + '.jpg', sha256:row.sha256, category:row.item.category, ...row.attribution });
  }
  const rows = [], labels = [];
  for (const [positive,negative,query] of selections) {
    for (const modality of ['text','photo','text+photo']) {
      for (const [candidate,isMatch] of [[positive,1],[negative,0]]) {
        const item = source.get(candidate);
        const row = [modality === 'photo' ? '' : query, modality === 'text' ? '' : 'match_photos/' + positive + '.jpg',item.item.description,'match_photos/' + candidate + '.jpg',isMatch];
        rows.push(row);
        labels.push({ csv_row:rows.length + 1, modality, query_image_id:positive, item_image_id:candidate, is_match:isMatch, basis:isMatch ? 'Same D1 source image/object (duplicate-photo baseline).' : source.get(positive).item.category === item.item.category ? 'Visually different object/appearance in the same source category.' : 'Visually different object and source category.' });
      }
    }
  }
  await writeFile(resolve(root,'match_pairs.csv'),[header,...rows].map(row => row.map(quote).join(',')).join('\n')+'\n');
  await writeFile(resolve(root,'match_pairs.provenance.json'),JSON.stringify({ source:'D1 Open Images validation subset', annotation:'Queries manually described from reviewed contact-sheet images; labels reflect source-object identity, not merely category similarity.', limitation:'Positive photo queries reuse item photos. No independent views, real owner queries or held-out evaluation are claimed.', photos, labels },null,2)+'\n');
  console.log('Wrote ' + rows.length + ' pairs and ' + photos.length + ' attributed photos.');
}

export async function validate() {
  const records = parse(await readFile(resolve(root,'match_pairs.csv'),'utf8'),{columns:true});
  const provenance = JSON.parse(await readFile(resolve(root,'match_pairs.provenance.json'),'utf8'));
  if (JSON.stringify(Object.keys(records[0])) !== JSON.stringify(header)) throw Error('Wrong CSV columns');
  if (records.length !== 102 || provenance.labels.length !== records.length) throw Error('Expected 102 documented pairs');
  const counts = {}, unique = new Set();
  for (const [index,row] of records.entries()) {
    const modality = row.query_text ? row.query_photo ? 'text+photo' : 'text' : row.query_photo ? 'photo' : '';
    if (!modality || !['0','1'].includes(row.is_match) || !row.item_description || !row.item_photo) throw Error('Invalid row ' + (index+2));
    const label = provenance.labels[index];
    if (label.csv_row !== index+2 || label.modality !== modality || String(label.is_match) !== row.is_match || row.item_photo !== 'match_photos/' + label.item_image_id + '.jpg' || (row.query_photo && row.query_photo !== 'match_photos/' + label.query_image_id + '.jpg')) throw Error('Provenance mismatch');
    if ((label.query_image_id === label.item_image_id) !== Boolean(label.is_match)) throw Error('Identity label mismatch');
    const signature = JSON.stringify(row); if (unique.has(signature)) throw Error('Duplicate pair'); unique.add(signature);
    const key=modality+'/'+row.is_match; counts[key]=(counts[key]||0)+1;
  }
  for (const modality of ['text','photo','text+photo']) for (const label of ['0','1']) if (counts[modality+'/'+label] !== 17) throw Error('Unbalanced modality');
  for (const photo of provenance.photos) {
    const bytes = await readFile(resolve(root,photo.photo_path));
    if (sha(bytes) !== photo.sha256 || bytes.length > 5*1024*1024 || bytes[0] !== 255 || bytes[1] !== 216) throw Error('Invalid photo: '+photo.photo_path);
    if (!photo.author || !photo.source_url || !photo.license.includes('/by/2.0')) throw Error('Missing attribution');
  }
  console.log(JSON.stringify({pairs:records.length, photos:provenance.photos.length, counts},null,2));
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const command=process.argv[2];
  if (command === 'build') await build();
  else if (command !== 'validate') throw Error('Usage: node match-pairs.mjs build|validate');
  await validate();
}
