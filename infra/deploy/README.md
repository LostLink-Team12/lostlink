# C3 dev deployment

Five service charts use a shared library for API and worker Deployments,
process-specific IRSA service accounts, HTTP/loop-progress probes, CPU HPAs,
and the PostgreSQL KEDA ScaledObject for matching tasks. The platform chart
provides the frontend gateway, private ALB Ingress, Prometheus, a restricted
SQL exporter and provisioned Grafana dashboard. Controller versions are fixed
in `controllers.ps1`; installed application images are ECR digests.

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
   a two-node capacity bound. Preserve this file for subsequent Terraform runs.
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
database pool has size 1 and no overflow. Claim outbox and Notification consume
validate only the configuration required by their mode. Item cleanup runs hourly
with loop health and only scans `staging/`, matching its existing IAM boundary.

## Routing and authentication

The browser uses one HTTPS origin, `/api/<service>/v1/*`, so PATCH, If-Match and
Idempotency-Key do not require cross-origin API requests. The gateway strips
only the service prefix and verifies TLS to ClusterIP APIs. It exposes no
`/internal/v1/*`, `/metrics`, OpenAPI or direct readiness paths. `/matches/*`
serves `/matches/index.html` with HTTP 200, preserving the browser's URL.
S3 uploads remain direct presigned POSTs with no Cognito Authorization header.

Use public clients with real Cognito code/PKCE login. Real user login, camera
permissions and UC1–UC5 must be checked against the deployed environment; a
successful unauthenticated route smoke does not establish these flows.

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
PostgreSQL query and HPA stabilization governs scale-down to one. The two-node
demo has API and task caps of two replicas; KEDA does not provision nodes.
Check allocatable CPU/memory, model cold starts and RDS connections before
raising those caps. Preserve controller conditions/events alongside load
summaries and same-time-range Grafana screenshots. Upload confirmation alone
does not establish embedding completion or search availability.

## Verification

```powershell
$env:HELM_BIN = (Resolve-Path infra/terraform/.terraform/tools/helm/windows-amd64/helm.exe).Path
python -m pytest -q infra/deploy/test_deployment.py
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
