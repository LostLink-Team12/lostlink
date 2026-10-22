# Load and scaling tests (owner: ZHANG JI)

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
The scaling demo scripts and evidence workflow are documented below under issue #10.

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

## Scaling demo (D / issue #10)

The scripts are implemented; **dev acceptance depends on C3 / issue #6**.
Use k6 **1.6.0** (the version used for local integration tests) and Node.js 22.12+.
Do not use k6 HTTP debug logging: request headers and presigned upload fields are
credentials. Default script metrics omit dynamic URLs and response bodies.

### 1. Check C3 before generating load

C3 must supply the HTTPS Item/Matching URLs, working Cognito owner/staff access
tokens, available seeded items, model weights, resource requests/limits, API HPA,
matching tasks PostgreSQL KEDA scaler, metrics-server and Prometheus/Grafana.
Pick an explicit kubectl context, namespace and common workload label selector.
The collector only reads Deployments, HPA and ScaledObjects; it never scales them.
Keep `/internal/v1/*` and `/metrics` private. Access Prometheus through an approved
local port-forward or private operator connection; do not expose it for this demo.

The current repository metrics are `lostlink_http_request_duration_seconds_bucket`
with `service`, `method`, `route`, `status` labels. Adjust `--p95-query` if C3 uses
different scrape labels. `--backlog-query` must be the actual C3-exported **single
aggregate series** for runnable embedding jobs (or the matching KEDA external metric),
not SQS depth. No backlog metric is fabricated by these scripts. Its underlying
SQL must follow ADR-0002:

```sql
SELECT count(*) FROM matching.work_jobs
WHERE state = 'pending' AND kind = 'embed' AND available_at <= now();
```

Also check DB leases/drain and controller events: delayed retries or in-flight
leases are not counted by that runnable-pending SQL. The API `consume` worker is
separate from CPU-heavy `tasks`. KEDA does not create node capacity; C3 must leave
room for replicas and account for model cold starts and RDS connection limits.

### 2. Search ramp: 0 → 25 → 50 → 100 → 200 → 0 VUs

From the repository root, first run `smoke.js`. Then:

```powershell
New-Item -ItemType Directory -Force tests/load/results/search | Out-Null
$env:MATCHING_API_URL = 'https://YOUR-DEV-MATCHING-API'
# Set LOSTLINK_ACCESS_TOKEN locally to a fresh owner access token.
$env:RUN_ID = 'search-demo-01'
$env:SUMMARY_PATH = 'tests/load/results/search/summary.json'
k6 run tests/load/search_load.js
if ($LASTEXITCODE -ne 0) { throw 'Search load failed; inspect the summary' }
```

The default 13-minute profile ramps for 2m, then targets 50/100/200 for 3m each,
then ramps down for 2m, with one second think time. `MAX_VUS`, `RAMP_DURATION`,
`HOLD_DURATION`, `THINK_SECONDS`, `SEARCH_TEXT`, `SEARCH_CATEGORY`, `EXPECT_ITEM_ID`
and `P95_MS` override it. Targets are interpolated ramp segments, not constant
hold plateaus. Thresholds are error rate <1%, every parsed search contract valid,
and p95 <5s (a demo target, not a production SLO). A single token means simulated
concurrent VUs for one owner, not 200 independent identities. Issue a token whose
remaining lifetime exceeds the run and graceful stop; an expired/forbidden token
aborts the test rather than presenting auth failures as a scalability result.

### 3. Import 5,000 photos and build embedding backlog

Preparation verifies every D1 checksum and creates a packed binary file so k6
reads one photo at a time instead of duplicating 1.4 GiB per VU. All files are
validated again during k6 setup **before the first API mutation**.

```powershell
node tests/load/prepare-load-data.mjs --count 5000
New-Item -ItemType Directory -Force tests/load/results/bulk | Out-Null
$env:ITEM_API_URL = 'https://YOUR-DEV-ITEM-API'
# Replace LOSTLINK_ACCESS_TOKEN locally with a fresh venue staff token.
$env:RUN_ID = 'bulk-demo-01'
$env:SUMMARY_PATH = 'tests/load/results/bulk/summary.json'
k6 run tests/load/bulk_import.js
if ($LASTEXITCODE -ne 0) { throw 'Bulk import incomplete; inspect summary and resume the same run' }
```

Defaults: 5,000 items, 10 concurrent VUs, 50 entries/batch, 45m maximum duration,
600s per-operation polling timeout. Overrides: `IMPORT_COUNT` (1–5000),
`IMPORT_VUS` (1–50), `BATCH_SIZE` (1–100), `IMPORT_DURATION`,
`POLL_TIMEOUT_SECONDS`, `LOAD_MANIFEST`, `LOAD_PHOTOS` (absolute paths recommended).
For a small dev rehearsal prepare `--count 10` and set `IMPORT_COUNT=10`.
Preflight token lifetime must cover the chosen maximum duration plus setup.

`RUN_ID` is mandatory, scoped to a stable ordered manifest/batch size. Use the
**same** ID, manifest and batch size to resume. API retries use identical
Idempotency-Key/body/If-Match. Already confirmed states are skipped and counted
separately in `photos_reused`. A fresh RUN_ID creates new demo inventory and new
work; choose it deliberately for a new scaling demonstration. No cleanup/delete
is automated. S3 uploads carry the presigned form fields and file last, without
the Cognito Authorization header. Every accepted photo operation is polled to
completed; terminal errors and timeouts fail the batch. Missing/partial iterations
fail the exact `photos_confirmed == IMPORT_COUNT` threshold. Report confirmation
and eventual search availability separately; 202 or S3 success is not embedding
completion. A rerun with 5,000 reused photos is not a fresh backlog demonstration.

### 4. Capture scaling evidence in parallel

Start this in a second terminal **before** either load run; keep it running after
load ends until HPA/KEDA cooldown and backlog drain are visible. Supply the actual
C3 labels and PromQL, not the example placeholders:

```powershell
node tests/load/capture-scaling.mjs --context YOUR-CONTEXT --namespace lostlink --selector 'YOUR-COMMON-LABEL=VALUE' --prometheus http://127.0.0.1:9090 --backlog-query 'YOUR_REAL_AGGREGATE_BACKLOG_PROMQL' --seconds 1200 --interval 15 --out tests/load/results/search
```

Use `--out tests/load/results/bulk` and a longer `--seconds` for the bulk run.
`--kubectl PATH` selects a non-PATH binary. Optional `PROMETHEUS_TOKEN` is read
from the environment only. JSON/HTML are saved after each sample; CSV is written
on normal completion. Missing CRDs, missing/ambiguous metric series or collection
errors produce exit code 2 and remain visible as unavailable; they never become
zero or passing evidence. No Secrets/Pod environments or raw kubectl errors are
saved. Keep clocks synchronized; observations use UTC ISO timestamps.

After each load/capture finishes:

```powershell
node tests/load/render-results.mjs --summary tests/load/results/search/summary.json --scaling tests/load/results/search/scaling.json --out tests/load/results/search/report
# Repeat with bulk paths for the import report.
```

Outputs: `report.md`, `key-numbers.json`, `report.html`; collector also writes
`scaling.json`, `scaling.csv`, `scaling.html`. Open the HTML for replica/backlog/p95
curves and screenshot it for the report. Also export Grafana screenshots at the
same UTC range, including the panel queries, and retain controller events. Search
p95 comes from the search summary; bulk HTTP p95 mixes uploads, polling and API
calls and must not be described as search latency.

Acceptance evidence must include: dev URL and image commit, start/end UTC,
k6 version/profile and run ID, request count/rate/p95/error rate, confirmed versus
reused photos, API and tasks desired/ready replica ranges, runnable backlog peak
and drain time, controller conditions/events, scale-up/cooldown screenshots, and
whether the load generator itself was saturated. Do not infer successful scaling
solely from increased replicas or missing data. `results/` is gitignored because
operator evidence can contain deployment identifiers. Copy only reviewed report
artifacts into the final demo/report handoff.

### Local checks (no AWS traffic)

```powershell
$env:K6_BIN = 'C:\PATH\TO\k6.exe'
node --test tests/load/*.test.mjs
```

CI downloads k6 1.6.0 and checks its official SHA256. Local integration tests run
real k6 against a localhost contract double, including multipart credential
separation, pending operations, exact counts, resume and failure paths. Tests skip
the real-k6 case when K6_BIN is absent. These are script correctness tests, not
performance results or proof of C3 deployment.

References: [k6 filesystem API](https://grafana.com/docs/k6/latest/javascript-api/k6-experimental/fs/),
[k6 HTTP](https://grafana.com/docs/k6/latest/javascript-api/k6-http/),
[scenario options](https://grafana.com/docs/k6/latest/using-k6/k6-options/reference/).
