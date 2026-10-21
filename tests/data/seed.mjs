import { parse } from 'csv-parse/sync';
import { readFile, writeFile, mkdir, rename } from 'node:fs/promises';
import { resolve, dirname, relative, isAbsolute } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { createApi, uploadPhoto, waitOperation, validatePhoto } from '../../frontend/src/api.js';

const SOURCE = 'https://storage.googleapis.com/openimages/';
const CLASSES = ['Backpack', 'Handbag', 'Briefcase', 'Suitcase', 'Luggage and bags', 'Wallet', 'Umbrella', 'Mobile phone', 'Laptop', 'Tablet computer', 'Computer keyboard', 'Computer mouse', 'Headphones', 'Camera', 'Glasses', 'Sunglasses', 'Watch', 'Clock', 'Book', 'Bottle', 'Footwear', 'Shoe', 'Hat', 'Scarf', 'Tie', 'Coat', 'Jacket', 'Clothing', 'Toy', 'Teddy bear', 'Sports equipment', 'Ball', 'Musical instrument', 'Pen', 'Mug'];
const license = /^https?:\/\/creativecommons\.org\/licenses\/by\/2\.0\/?$/;
const hash = value => createHash('sha256').update(typeof value === 'string' || Buffer.isBuffer(value) ? value : JSON.stringify(value)).digest('hex');

export function selectItems(images, labels, classes, count = 5000) {
  if (count !== null && (!Number.isInteger(count) || count < 1)) throw Error('Count must be a positive integer');
  const candidates = new Map();
  for (const label of labels) {
    const category = classes.get(label.LabelName);
    if (label.Confidence !== '1' || !CLASSES.includes(category)) continue;
    const current = candidates.get(label.ImageID);
    if (!current || CLASSES.indexOf(category) < CLASSES.indexOf(current)) candidates.set(label.ImageID, category);
  }
  const rows = images.filter(image => candidates.has(image.ImageID) && /^[a-f0-9]{16}$/.test(image.ImageID) && license.test(image.License) && image.Author && image.OriginalLandingURL)
    .sort((a, b) => a.ImageID.localeCompare(b.ImageID)).slice(0, count ?? undefined).map(image => {
      const category = candidates.get(image.ImageID);
      const attribution = { author: image.Author, author_url: image.AuthorProfileURL || '', title: image.Title || '', source_url: image.OriginalLandingURL, license: image.License, changes: 'None; original Open Images validation image.' };
      return {
        client_reference: `oi-validation-${image.ImageID}`, photo_path: `photos/${image.ImageID}.jpg`,
        download_url: `https://s3.amazonaws.com/open-images-dataset/validation/${image.ImageID}.jpg`,
        attribution,
        item: { title: `${category} · ${image.ImageID.slice(-6)}`, description: `Demo found ${category.toLowerCase()}. Context photo from Open Images validation. Photo: ${attribution.title.slice(0, 150)} by ${attribution.author.slice(0, 150)}. Source: ${attribution.source_url}. License: ${attribution.license}. No modifications.`.slice(0, 2000), category, found_at: '2026-10-17T04:00:00Z', public_location_hint: 'Demo venue lost-and-found counter', verification_questions: [{ prompt: 'What is the demo reference printed on your receipt?', reference_answer: image.ImageID.slice(-6) }] },
      };
    });
  if (count !== null && rows.length !== count) throw Error(`Only ${rows.length} eligible licensed item photos; requested ${count}. No duplicates or unlicensed substitutes were added.`);
  return rows;
}

async function fetchBytes(url, attempts = 3) {
  for (let n = 0; n < attempts; n++) {
    try {
      const result = await fetch(url, { signal: AbortSignal.timeout(60000), redirect: 'error' });
      if (!result.ok) throw Error(`Download HTTP ${result.status}`);
      return Buffer.from(await result.arrayBuffer());
    } catch (error) {
      if (n + 1 === attempts) throw error;
      await new Promise(r => setTimeout(r, 500 * (n + 1)));
    }
  }
}

async function cached(root, name, url) {
  const path = resolve(root, name);
  try { return await readFile(path); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const bytes = await fetchBytes(url);
  await writeFile(`${path}.part`, bytes); await rename(`${path}.part`, path);
  return bytes;
}

export async function prepare(root, count = 5000, metadataOnly = false) {
  await mkdir(root, { recursive: true }); await mkdir(resolve(root, 'photos'), { recursive: true });
  const [images, labels, names] = await Promise.all([
    cached(root, 'images.csv', SOURCE + '2018_04/validation/validation-images-with-rotation.csv'),
    cached(root, 'labels.csv', SOURCE + 'v5/validation-annotations-human-imagelabels-boxable.csv'),
    cached(root, 'classes.csv', SOURCE + 'v7/oidv7-class-descriptions-boxable.csv'),
  ]);
  if (!Number.isInteger(count) || count < 1) throw Error('Count must be a positive integer');
  const candidates = selectItems(parse(images, { columns: true, skip_empty_lines: true }), parse(labels, { columns: true, skip_empty_lines: true }), new Map(parse(names, { skip_empty_lines: true })), null);
  if (candidates.length < count) throw Error(`Only ${candidates.length} eligible photos for requested ${count}`);
  console.log(`Found ${candidates.length} unique CC BY 2.0 item candidates with attribution.`);
  const rows = [];
  if (metadataOnly) rows.push(...candidates.slice(0, count));
  else for (let start = 0; rows.length < count && start < candidates.length; start += 8) {
    const prepared = await Promise.all(candidates.slice(start, start + 8).map(async row => {
      const bytes = await cached(root, row.photo_path, row.download_url);
      // Original CVDF images can exceed the backend's 10 MiB upload ceiling.
      // Replace these deterministically with the next licensed candidate.
      if (bytes[0] !== 0xff || bytes[1] !== 0xd8 || bytes.length > 10485760) { console.log(`Skipped unsupported photo: ${row.client_reference}`); return null; }
      return { ...row, sha256: hash(bytes) };
    }));
    const previous = rows.length;
    rows.push(...prepared.filter(Boolean).slice(0, count - rows.length));
    if (Math.floor(previous / 100) !== Math.floor(rows.length / 100)) console.log(`Photos ready: ${rows.length}/${count}`);
  }
  if (rows.length !== count) throw Error(`Only ${rows.length} supported photos were downloaded`);
  await writeFile(resolve(root, 'manifest.json'), JSON.stringify(rows, null, 2) + '\n');
  await writeFile(resolve(root, 'ATTRIBUTION.json'), JSON.stringify(rows.map(row => ({ photo_path: row.photo_path, ...row.attribution })), null, 2) + '\n');
  console.log(metadataOnly ? `Selected ${rows.length} photos (metadata only).` : `Prepared ${rows.length} photos. Import through item-service to upload them to private S3 and enqueue embeddings.`);
  return rows;
}

export async function importItems(rows, api, upload = uploadPhoto, readPhoto, { timeout = 600000, log = () => {} } = {}) {
  if (!rows.length || new Set(rows.map(row => row.client_reference)).size !== rows.length) throw Error('Manifest requires nonempty, unique references');
  let confirmed = 0;
  for (let start = 0; start < rows.length; start += 100) {
    const batch = rows.slice(start, start + 100);
    const batchKey = hash(batch.map(row => ({ reference: row.client_reference, item: row.item, sha256: row.sha256 })));
    const job = await api('item', '/v1/item-imports', { body: { batch_reference: batchKey, entries: batch.map(row => ({ client_reference: row.client_reference, ...row.item })) }, key: `batch-${batchKey}` });
    let status;
    const deadline = Date.now() + timeout;
    do {
      status = await api('item', `/v1/item-imports/${encodeURIComponent(job.id)}?limit=100`);
      if (status.job.accepted + status.job.failed === status.job.total) break;
      if (Date.now() > deadline) throw Error(`Import ${job.id} is still pending`);
      await new Promise(r => setTimeout(r, 1000));
    } while (true);
    if (status.job.failed || status.items.length !== batch.length || status.has_more) throw Error(`Import ${job.id} has failed or incomplete entries; inspect its status before retrying`);
    for (const entry of status.items) {
      const row = batch.find(row => row.client_reference === entry.client_reference);
      if (!row || entry.state !== 'succeeded') throw Error('Unexpected import result');
      const path = `/v1/items/${encodeURIComponent(entry.item_id)}`;
      const item = await api('item', path);
      if (['processing', 'available', 'reserved', 'handed_over'].includes(item.state)) { confirmed++; continue; }
      if (item.state !== 'awaiting_upload') throw Error(`Item ${entry.item_id}: ${item.state}`);
      const photo = await readPhoto(row);
      const key = hash([entry.item_id, row.sha256 || '', item.version]);
      const target = await api('item', `${path}/uploads`, { body: { content_type: 'image/jpeg', size: photo.size }, version: item.version, key: `upload-${key}` });
      await upload(target.upload, photo);
      const operation = await api('item', `${path}/uploads/${encodeURIComponent(target.upload_id)}/confirm`, { body: {}, key: `confirm-${target.upload_id}` });
      await waitOperation(api, 'item', operation.operation_id, { attempts: Math.ceil(timeout / 1000) });
      confirmed++;
    }
    log({ processed: Math.min(start + 100, rows.length), confirmed });
  }
  return confirmed;
}

async function main() {
  const [command, ...args] = process.argv.slice(2);
  const option = (name, fallback) => { const index = args.indexOf(name); return index < 0 ? fallback : args[index + 1]; };
  const root = resolve(option('--dir', resolve(dirname(fileURLToPath(import.meta.url)), 'generated')));
  if (command === 'prepare') return prepare(root, Number(option('--count', '5000')), args.includes('--metadata-only'));
  if (command !== 'import') throw Error('Usage: node seed.mjs prepare [--count 5000] [--dir PATH] [--metadata-only] | import --api-url URL [--dir PATH]');
  const url = new URL(option('--api-url', ''));
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) throw Error('Use HTTPS or a local Item API');
  if (url.username || url.password || url.search || url.hash) throw Error('Use a fixed Item API base URL');
  const token = process.env.LOSTLINK_ACCESS_TOKEN;
  if (!token) throw Error('Set LOSTLINK_ACCESS_TOKEN to a venue staff access token');
  const rows = JSON.parse(await readFile(resolve(root, 'manifest.json'), 'utf8'));
  const readPhoto = async row => {
    const path = resolve(root, row.photo_path), within = relative(root, path);
    if (within.startsWith('..') || isAbsolute(within)) throw Error('Photo path must remain inside the dataset directory');
    const bytes = await readFile(path);
    if (!row.sha256 || hash(bytes) !== row.sha256) throw Error(`Photo checksum mismatch: ${row.client_reference}`);
    if (bytes[0] !== 0xff || bytes[1] !== 0xd8) throw Error(`Photo is not a JPEG: ${row.client_reference}`);
    const photo = new Blob([bytes], { type: 'image/jpeg' });
    validatePhoto(photo, 10);
    return photo;
  };
  // Validate every file before the first backend mutation.
  for (const row of rows) await readPhoto(row);
  const confirmed = await importItems(rows, createApi({ item: url.href.replace(/\/$/, '') }, () => token), uploadPhoto, readPhoto, { log: value => console.log(JSON.stringify(value)) });
  console.log(`Confirmed ${confirmed} photos in S3; embedding availability must be checked separately.`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main().catch(error => {
  // Avoid printing bearer tokens or signed upload URLs from network errors.
  console.error(`Dataset command stopped: ${error.name}: ${error.message.includes('https:') ? 'Network or configuration error' : error.message}`); process.exitCode = 1;
});
