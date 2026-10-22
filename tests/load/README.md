# Search validation and scaling tests

## Issue #7 backend investigation

`search_review.js` is the dedicated read-only 500 ms p95 investigation for Issue #7.
It has separate warmup/measurement, required sample counts, text/photo/fused cases,
known-result and stale-result assertions, and aggregate JSON evidence.
See [the reproducible runbook](../../docs/search-review-performance.md) for local
real-CLIP/pgvector/HTTP fixtures, AWS dev runs and the batch/cache decision criteria.
`node --test tests/load/search_review.test.mjs` checks its contract without dependencies.
CI also exercises the real pinned k6 runner against controlled HTTP failure cases;
this checks the tooling, not application performance.

## Issue #10 scaling demo (owner: ZHANG JI)

k6 scripts used in the demo:

`smoke.js` is the Phase 1 read-only API smoke test (one owner, five text searches).
Install k6, start matching-service with its workers/model and a seeded available item,
then in PowerShell:

```powershell
# Run from the repository root.
$env:MATCHING_API_URL = "https://DEV-MATCHING-API"
# Set LOSTLINK_ACCESS_TOKEN through your local secret/session workflow (owner access token).
$env:SEARCH_TEXT = "backpack"
# Optional: EXPECT_ITEM_ID asserts a particular seeded item appears in the top five.
k6 run tests/load/smoke.js
```

No token, response body, claim or hidden verification answer is logged. A missing
configuration, HTTP failure, invalid response or failed threshold exits nonzero.
Default p95 < 5s is a smoke threshold, not a production performance target.
The following scaling scenarios belong to the later scaling demo and are not yet implemented:

- `search_load.js` - ramp owners searching across venues; shows HPA scaling the API pods and p95 latency
- `bulk_import.js` - upload 5,000 found-item photos; PostgreSQL `work_jobs` backlog makes KEDA scale embedding task workers out and back in (SQS consumer scaling is a separate concern)

## Demo dataset and import (D1)

Requires Node.js 22.12+. From the repository root:

```powershell
Push-Location tests/data
npm ci --ignore-scripts
node seed.mjs prepare --count 5000
# Set LOSTLINK_ACCESS_TOKEN to a venue staff access token in your local session.
node seed.mjs import --api-url https://DEV-ITEM-API
npm test
Pop-Location
```

Preparation uses [official Open Images metadata](https://storage.googleapis.com/openimages/web/download_v7.html)
to select positive human-verified portable-item labels. It accepts per-image CC BY 2.0
records with an author and source landing page, retains attribution, and downloads
unmodified context photos containing the labeled objects. These are not cropped
product photos. Source license records are not a fresh verification of each creator's
live page. Open Images annotations are CC BY 4.0; retain dataset credit on redistribution.

Output is `tests/data/generated/` (gitignored): photos, `manifest.json` with SHA256
checksums, and `ATTRIBUTION.json`. Preparation skips unsupported/over-10-MiB images
and fills from later candidates. The locally verified 5,000-image set occupies about
1.41 GiB. Reruns reuse cached downloads; `--dir PATH` changes the output directory.
`--count 10` supports a small run. `--metadata-only` writes a selection without
photo checksums: run preparation without that flag before importing. Keep the same
manifest/count when resuming an import.

Import uses the `phase1-backend` item contract: batches of up to 100, stable batch
references/keys, versioned S3 form uploads and confirmation polling. It checks all
local files before the first API write and skips already confirmed items on rerun.
The Cognito token is sent only to the Item API. Attribution is included in public
item descriptions; found times, location hints and receipt verification answers are
synthetic demo data. Public demo answers must not serve as real owner verification.

Import requires a deployed Item API, staff account/membership, private S3 and workers.
It uses presigned URLs and needs no local AWS access keys. Photo confirmation does
not mean embedding is complete: wait for item state `available` before the smoke test.
Use a separate staff token/run for each venue. S3 import and live k6 acceptance have
not been verified against AWS dev.
