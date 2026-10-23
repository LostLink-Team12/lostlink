# C3 dev deployment

Five service charts use a shared library for API and worker Deployments,
process-specific IRSA service accounts, HTTP/loop-progress probes, CPU HPAs,
and the PostgreSQL KEDA ScaledObject for matching tasks. The platform chart
provides the frontend gateway, private ALB Ingress, Prometheus, a restricted
SQL exporter and provisioned Grafana dashboard. Controller versions are fixed
in `controllers.ps1`; installed application images are ECR digests.

The separately merged `infra/helm/lostlink` umbrella chart from #17 remains
available with its original contract checks. The live deployment described here
uses five per-service releases plus `platform`, as requested in #6. They share
process service-account identities: **do not install both variants into the same
namespace**. The operator renderer reads #17's matching threshold values directly
and preserves its 4 GiB model memory floor. Both durable matching paths consume
`MATCH_THRESHOLD_TEXT` for text-only reports and `MATCH_THRESHOLD` for reports
with a photo. If the text setting is absent, the old single-threshold behavior
is preserved. This connects #17's values; it does not re-calibrate the dataset.

## Prerequisites

- Apply the application AWS configuration from `../terraform/APPLICATION.md`.
- A valid `lostlink-dev` AWS profile, an allowlisted EKS API address, Terraform,
  Docker, Node.js 22.12+ and Python 3.12 are required.
- Install `requirements-dev.lock`, `./libs` and `PyYAML==6.0.3` in an isolated
  Python environment. The commands below use `.venv-c3/Scripts/python.exe`.
- Install checksum-verified Helm 4.3.1 at
  `../terraform/.terraform/tools/helm/windows-amd64/helm.exe` and kubectl at
  `../terraform/.terraform/tools/kubectl.exe`. Tool binaries and operator
  artifacts are ignored. CI independently verifies the Linux Helm checksum.
- Use the repository-local kubeconfig at
  `../terraform/.terraform/validation/kubeconfig`, context `lostlink-dev`.

Run all commands from the repository root. Never print state, secret-bearing
Helm values, raw Kubernetes Secret objects, request tokens or S3 upload fields.

## Deploy

1. Run `python infra/deploy/configure.py infrastructure`. This creates ignored
   `.local/infrastructure.tfvars.json`, including a random origin header and
   two baseline nodes and a five-node demo ceiling. Preserve this file for
   subsequent Terraform runs.
2. Plan and apply dev Terraform with
   `-var-file=../../../deploy/.local/infrastructure.tfvars.json` when using
   `-chdir=infra/terraform/environments/dev`. Review the saved plan first.
   This enables the ALB controller IRSA role, frontend ECR and private-origin
   security group. It preserves existing C1/C2/application resources.
3. Run `python infra/deploy/configure.py prepare`, then
   `./infra/deploy/controllers.ps1`. Preparation generates internal TLS material
   in memory and stores the server key only in a Kubernetes Secret; it mounts
   public CAs and the official RDS CA bundle. Existing internal certificates
   are reused and fail explicitly when renewal is due.
4. Commit application changes, then run
   `python infra/deploy/build.py --tag <full-source-commit>`.
   The Matching build reuses the existing published ML dependency layer by
   digest and installs current application/shared-library code. It does not
   use an unpinned ML base. The build writes digest/source evidence under
   `.local/`. A temporary ECR login config is removed on exit, without changing
   the user's Docker credentials.
5. Before starting applications, run `bootstrap-app.py` with the same image SHA
   as documented in `APPLICATION.md`. It runs five migration Jobs using separate
   schema-owner credentials and verifies all 15 runtime database roles. Migration
   credentials and temporary database CREATE grants are removed in its finalizer.
   The application charts also provide optional migration hooks: supply an
   operator-provisioned, schema-specific `migration.existingSecret` containing
   `MIGRATION_DATABASE_URL`; never reuse it as a runtime secret. The operator must
   handle the temporary CREATE grant required by existing Alembic environments.
6. Run `python infra/deploy/configure.py monitor-role`. Its temporary admin Job
   creates `lostlink_matching_monitor` with SELECT on only `state`, `kind`,
   `available_at` in `matching.work_jobs`, an explicit SELECT RLS policy, a
   read-only default and a statement timeout. The credential is shared only
   by KEDA and the private exporter, never an application worker or API.
7. Run `python infra/deploy/configure.py values`, then
   `./infra/deploy/releases.ps1`. Before an HTTPS origin exists, Notification
   tasks are omitted entirely. No fake sender origin or credentials are injected.
8. Wait for the private ALB to become active; run `python infra/deploy/edge.py`.
   Plan and apply Terraform again with the same ignored var-file. This creates
   a CloudFront VPC origin and HTTPS distribution and adds its exact origin/
   callback/logout URLs to Cognito and S3 CORS. CloudFront provisioning may take
   several minutes. The ALB stays in private subnets and accepts the restricted
   CloudFront origin traffic, with an additional random header condition.
9. Regenerate values and run `releases.ps1` after the HTTPS origin is available.
   Notification tasks now receive the real `OWNER_APP_BASE_URL`. Check all
   Deployments and HPA/KEDA conditions; `helm` reporting deployed does not prove
   application readiness. Run `python infra/deploy/smoke.py` for real HTTPS and
   private-route checks.

The normal scripts refuse the wrong AWS account/cluster before transferring
credentials. Runtime launchers read only their process's authorized secret ARNs
via IRSA, construct TLS-verified database URLs in memory, and exec PID 1. Each
database pool has size 1 and no overflow, except Item API's separate internal
pool, which has size 2; its user-facing pool stays at 1. The observed dev RDS
limit is 79 connections, including 3 reserved. At all configured HPA/KEDA caps
(including the explicit eight-Matching-API benchmark override), application
pools budget 64, monitoring 2, RDS/background 8 and operators 2, leaving the
3 reserved connections. Do not run migrations concurrently with this profile.
The deployment contract test rejects expanding past this envelope.
Claim outbox and Notification consume
validate only the configuration required by their mode. Item cleanup runs hourly
with loop health and only scans `staging/`, matching its existing IAM boundary.

## Routing and authentication

The browser uses one HTTPS origin, `/api/<service>/v1/*`, so PATCH, If-Match and
Idempotency-Key do not require cross-origin API requests. The gateway strips
only the service prefix and verifies TLS to ClusterIP APIs. It exposes no
`/internal/v1/*`, `/metrics`, OpenAPI or direct readiness paths. `/matches/*`
serves `/matches/index.html` with HTTP 200, preserving the browser's URL.
S3 uploads remain direct presigned POSTs with no Cognito Authorization header.

Matching reuses a verified SSL context for Item reads. Every request still
performs a new HTTPS exchange with current authorization and Item state;
responses are not cached. Restart Matching API/tasks after changing the CA
trust bundle. Candidate scoring/sorting remains in PostgreSQL; complete dense
vectors are no longer transferred for results that only need version metadata.
Unfiltered retrieval uses a distance-only bounded inner query so PostgreSQL can
use HNSW, then orders that candidate set by distance and item ID. Eligibility and
RLS checks remain inside the bound. Selective category/venue/date queries retain
exact retrieval because pgvector 0.7 cannot iteratively recover filtered hits.
Equal-score membership at the candidate cutoff can differ under approximate
retrieval; the existing truncation indicator remains in the response.

Use public clients with real Cognito code/PKCE login. Real user login, camera
permissions and UC1–UC5 must be checked against the deployed environment; a
successful unauthenticated route smoke does not establish these flows.

`acceptance.py seed` creates isolated synthetic Cognito identities without sending
invitations. Their random passwords remain in `lostlink-acceptance`, never local
files or output. Trusted fixture email flags do not demonstrate mailbox ownership
verification. For `acceptance.py flow`, pause `notification-tasks` first; the
script refuses a running sender, including a terminating Pod after its replica
target reaches zero. Wait for Pod deletion before running fixtures:
`kubectl --context lostlink-dev -n lostlink wait --for=delete pod -l app.kubernetes.io/name=notification-tasks --timeout=180s`.
It tests real code/PKCE, two-venue onboarding,
S3 upload/sealing, CLIP embedding/search, report matching and the notification
inbox, unverified-owner rejection, version conflicts, approval and QR payload
handover. Close all synthetic fixture reports before restoring the sender. A
failed run can leave fixture records; inspect them using that fixture's account.
No existing person's password or group membership is changed. The fixture's
temporary platform privilege is removed in a finalizer after onboarding.

## Monitoring and scaling acceptance

Both monitoring UIs are ClusterIP-only. Use an explicit context and namespace:

```powershell
kubectl --context lostlink-dev -n lostlink port-forward service/prometheus 9090:9090
kubectl --context lostlink-dev -n lostlink port-forward service/grafana 3000:3000
```

Grafana's random admin password is in Secret `lostlink-grafana`; retrieve it
privately. The provisioned dashboard UID is `lostlink-c3-scaling`. It shows
desired/available replicas, runnable/leased/delayed/failed embedding jobs, API
p95, error ratio, request rate and scrape availability. Missing scrapes fail,
and are never converted to zero. Demo metrics storage uses bounded emptyDir
volumes; export evidence before restarting or removing monitoring pods.

Use `tests/load/README.md` for search/import execution. Handoff parameters:

- context `lostlink-dev`, namespace `lostlink`, selector
  `app.kubernetes.io/part-of=lostlink`;
- private Prometheus `http://127.0.0.1:9090` through port-forward;
- backlog PromQL `lostlink_embedding_runnable_jobs` (one aggregate series);
- Item API `<frontend_origin>/api/item`, Matching API
  `<frontend_origin>/api/matching`;
- fresh owner/staff Cognito access tokens covering the entire run.

The scaler's runnable SQL is fixed in ADR-0002 and the chart. It excludes
delayed retries and in-flight leases, which the dashboard exposes separately.
Matching tasks keep one warm replica; KEDA handles replica changes from the
PostgreSQL query and HPA stabilization governs scale-down to one. The baseline
has two nodes. Matching API and tasks each cap at two, and each model pod reserves
and limits 4 GiB, retaining #17's verified memory floor. Item API caps at eight
after real search exposed its internal lookup bottleneck; other APIs cap at two.
The two-node baseline supports the minimum two model pods, not the combined
HPA/KEDA ceiling. Four model pods require four t3.large model slots plus capacity
for non-model processes. KEDA does not provision nodes.
Check allocatable CPU/memory, model cold starts and RDS connections before
raising those caps. Preserve controller conditions/events alongside load
summaries and same-time-range Grafana screenshots. Upload confirmation alone
does not establish embedding completion or search availability.

The first two-node, two-API-replica run failed its 5s p95 target. Subsequent text
profiling temporarily used five t3.large nodes and eight Matching API replicas
with 2500 MiB requests / 4 GiB limits, before #17's safer 4 GiB reservation was
merged. Those historical runs are not a maximum-photo capacity guarantee.
The final templates reject that lower reservation. `load.py search` now requires
an explicit eight-replica HPA and at least nine reserved 4 GiB model slots,
plus room for other processes. Five t3.large nodes cannot provide those slots;
provision larger/more nodes and inspect actual placement before repeating it.
Use an explicit Helm `--set hpa.maxReplicas=8` override only with that capacity.
Keep all results distinct. After load, backlog drain and cooldown, restore the
default HPA cap and return `node_desired_size` to 2 through a reviewed Terraform
plan. This is deliberate operator capacity management; no node autoscaler is installed.

`preserve-monitoring.py protect` temporarily protects the two original baseline
instances before removing the temporary demo nodes, so their private monitoring
emptyDir histories survive that scale-in. Run `preserve-monitoring.py restore`
afterwards to restore their previous protection flags. This is not durable
storage or protection against node failure; keep exported report artifacts.

`load.py bulk --run-id <stable-id>` checks actual available item states after
embedding/lease drain, separately from the 5,000-photo confirmation counter.
The first bulk acceptance also ran a real owner search canary every 30 seconds;
its low-rate timings are separate from k6's bulk HTTP metrics. Future bulk
captures use `--no-search-p95` when no concurrent searches are generated.

For a retry, keep `--run-id` unchanged and supply a new `--evidence-id`; this
preserves the failed attempt's evidence and its deterministic import identities.
The collector requires runnable/leased/delayed jobs to drain, rejects any increase
in historical failed jobs, and verifies actual Item states after bulk load.
It does not delete failed audit records or count them as drained work.

The initial D1 import found five MPO originals and two oversized photos (one
overlaps the MPO group), affecting six items. `repair-load-photos.py` is limited
to failed items in the recorded synthetic C3 venue. With the locked Matching
`Pillow==13.0.0`, it uploads first-frame JPEG derivatives bounded to 2048 pixels
through Item's normal versioned API. Original files, source attribution, hashes,
derived hashes and historical failures are retained. Review `.local/photo-repairs.json`.

This dev-only CloudFront distribution intentionally defers a paid WAF policy;
the scoped Trivy exception is documented beside that resource. Provision a
reviewed WAF policy before using this as a production service. Deployments in
this change are operator-driven; `c3.yml` validates charts, not cloud CD.

## Verification

```powershell
$env:HELM_BIN = (Resolve-Path infra/terraform/.terraform/tools/helm/windows-amd64/helm.exe).Path
python -m pytest -q infra/deploy
python -m pytest -q -m 'not integration and not model and not scale'
```

The deployment tests instantiate every process's real backend settings and
render all five charts, asserting isolated identities, probes and the SQL scaler.
`.github/workflows/c3.yml` runs these checks independently of AWS. Load script
tests run actual checksum-verified k6 against localhost; they verify S3 file-last
ordering on all platforms and do not substitute for cloud performance evidence.

Keep the C3 deployment record in `docs/reviews/` accurate about actual completed
checks and remaining human/device-dependent acceptance. Do not close #6 based
only on manifest rendering or the AWS resource deployment.
