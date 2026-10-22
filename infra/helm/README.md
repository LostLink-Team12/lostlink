# LostLink Helm chart (C3)

Owner: YANG DONGWEI (chart), LU JINGXING (cluster operation). Issue #6.

`infra/helm/lostlink` deploys every backend process from
[team alignment §4](../../docs/team-alignment.md) onto the C1 EKS cluster:

| Service | Processes (one Deployment and service account each) |
| --- | --- |
| venue | `venue-api`, `venue-outbox`, `venue-identity` |
| item | `item-api`, `item-tasks`, `item-outbox`, `item-consume`, `item-cleanup` |
| matching | `matching-api`, `matching-tasks` (KEDA), `matching-consume`, `matching-outbox` |
| claim | `claim-api`, `claim-tasks`, `claim-outbox` |
| notification | `notification-api`, `notification-consume`, `notification-tasks` |
| delivery | `delivery-api` (Seed 2 skeleton, disabled by default) |

The 18 enabled processes match the IRSA identities in
`infra/terraform/environments/dev/application-iam.tf` (`codex/app-aws-config`) one to one.

## Design

- **Secrets stay in Secrets Manager.** Each container starts through
  `python -m lostlink_common.secret_env -- <command>`. The pod's own IRSA role reads only
  the secrets listed in its `LOSTLINK_SECRET_ENV`, builds `postgresql+psycopg` URLs with
  `sslmode=verify-full` and the RDS CA in memory, then `exec`s the process. No Kubernetes
  Secret, file or Terraform state holds a database password. Migration credentials are never
  mapped into API or worker pods.
- **Internal HTTPS.** Backend settings reject non-HTTPS service URLs. The chart creates a
  private CA (kept across upgrades) and one certificate per API Service. APIs serve TLS on
  8443 (`<service>-service:443`); an init container appends the CA to the system bundle and
  `SSL_CERT_FILE` points at it, so `http.client` verifies peers and still trusts Cognito/AWS.
- **Autoscaling.** APIs have CPU HPAs (1–2 replicas; search runs CLIP in-request,
  ADR-0007). `matching-tasks` scales with a KEDA `postgresql` trigger on pending `embed`
  jobs in `matching.work_jobs`, about 20 per replica, `minReplicaCount: 1` (ADR-0002 as
  amended). Deployments managed by an HPA or KEDA omit `replicas`.
- **CLIP weights.** `matching-api` and `matching-tasks` download the pinned checkpoint in an
  init container, verify SHA256 and mount it read-only; the app verifies it again.
- **Ingress (optional).** One ALB Ingress lists only public `/v1/...` prefixes, so
  `/internal/v1/*`, `/metrics`, `/health` and `/ready` are never exposed.
- **Pod security.** Non-root UID 1000, read-only root filesystem, no privilege escalation,
  all capabilities dropped, `RuntimeDefault` seccomp, memory limits on every container.

## Prerequisites

1. C1 applied with `application_runtime_enabled = true`, and `bootstrap-app.py` completed
   (it creates the database roles, runs all five migrations and fills the secrets).
2. Images published from a `main` commit that contains `lostlink_common.secret_env` and the
   worker-mode validation fix in this PR. Use that full commit SHA as `image.tag`.
3. Cluster add-ons:
   - `metrics-server` (EKS add-on) for the HPAs.
   - KEDA ≥ 2.17 in namespace `keda`, with an IRSA role for `keda:keda-operator` that can
     read only `lostlink-dev/database/matching/worker` (Terraform below). This reuses the
     Matching worker login, whose RLS policy can count all jobs; a dedicated read-only
     scaler role needs a migration change and is tracked as a follow-up.
   - Optional: AWS Load Balancer Controller, only if `ingress.enabled=true`.
4. Pods need outbound HTTPS (public node subnets already provide it) for the model download.

KEDA operator role, to add next to `application-iam.tf`:

```hcl
resource "aws_iam_role" "keda_operator" {
  count = var.application_runtime_enabled ? 1 : 0
  name  = "${local.name}-keda-operator"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = "system:serviceaccount:keda:keda-operator"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "keda_operator" {
  count = var.application_runtime_enabled ? 1 : 0
  role  = aws_iam_role.keda_operator[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"],
        Resource = aws_secretsmanager_secret.application["database/matching/worker"].arn },
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.dev.arn,
        Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.aws_region}.amazonaws.com" } } }
    ]
  })
}
```

```powershell
helm repo add kedacore https://kedacore.github.io/charts
helm upgrade --install keda kedacore/keda -n keda --create-namespace --version ">=2.17.0 <3.0.0" `
  --set podIdentity.aws.irsa.enabled=true `
  --set podIdentity.aws.irsa.roleArn=<keda-operator role ARN>
aws eks create-addon --cluster-name lostlink-dev --addon-name metrics-server
```

## Deploy

From the repository root, with the C1 kubeconfig:

```powershell
terraform -chdir=infra/terraform/environments/dev output -json > tf-outputs.json
python infra/helm/render_values.py tf-outputs.json --image-tag <full-commit-sha> `
  --owner-app-url https://<owner-app-origin> > values-dev.generated.json
curl.exe -fsSLo global-bundle.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
helm upgrade --install lostlink infra/helm/lostlink -n lostlink --create-namespace `
  -f values-dev.generated.json --set-file database.caBundle=global-bundle.pem --wait --timeout 15m
```

`render_values.py` copies only IDs, ARNs and URLs from the Terraform outputs. Do not commit
`tf-outputs.json` or the generated values. The chart refuses to render when a required input
is missing, when the namespace is not `lostlink` (IRSA trust is bound to it) or when the
Owner App origin is not HTTPS.

Check:

```powershell
kubectl -n lostlink get deploy,pods,hpa,scaledobject
kubectl -n lostlink logs deploy/matching-api -c fetch-model
kubectl -n lostlink port-forward svc/item-service 8443:443   # then curl -k https://localhost:8443/ready
```

`/ready` returns 503 until configuration, migrations and dependencies are ready, so a pod
that stays unready points at a missing permission or input; its log names the setting.

## Capacity on the demo node

One `t3.large` (2 vCPU, 8 GiB). The 18 processes request about 1.0 vCPU and 4.7 GiB; the two
CLIP processes take 1 GiB each. KEDA, metrics-server and system pods use most of the rest, so
`maxReplicas` is 2 for Matching. For the load-test run, raise `node_max_size`/`node_desired_size`
to 2 first (C1 README); KEDA scales pods, not EC2 nodes.

## Validation

CI job `helm-chart` runs `helm lint`, `helm template`, kubeconform (Kubernetes and KEDA CRD
schemas) and `tests/check_rendered.py`, which checks the rendered manifests against the C1
IAM contract (no process asks for a secret its role cannot read), the backend startup
settings for each process, HTTPS service URLs, autoscaling ownership and Ingress exposure.

## Open items

- Owner App / Venue Portal hosting and their HTTPS origins (Cognito callbacks, S3 CORS,
  `OWNER_APP_BASE_URL`) — ZHANG JI and LU JINGXING.
- ALB: domain and ACM certificate; without a certificate the Ingress listens on HTTP only.
- Prometheus/Grafana (C3 monitoring); pods already carry scrape annotations for `/metrics`.
- Dedicated read-only KEDA database role (needs a Matching migration and a C1 secret).
