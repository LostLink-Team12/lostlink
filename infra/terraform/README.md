# AWS dev infrastructure (C1)

Owner: LU JINGXING. Two Terraform roots provision the Phase 1 environment:

- `bootstrap/`: state bucket, versioning, KMS encryption, private access and TLS.
- `environments/dev/`: VPC across two AZs, EKS, RDS PostgreSQL 16, ECR, media S3,
  SQS/SNS, Cognito, workload IAM roles and account-wide budget alerts.

## Deployment verified on 22 October 2026

Both roots have been applied to account `152174417545` in `ap-southeast-1` using
the `lu-jingxing` IAM user. Bootstrap created 8 resources. Dev imported the
existing C2 ECR repository, created 112 resources, updated its scanning/tags, and
deleted none. These counts include IAM/network configuration, not 112 servers.
A follow-up added the `delivery-service` ECR repository and lifecycle policy
after rebasing onto current main, so every current CI service has a registry.

- `lostlink-dev`: EKS 1.36, one `t3.large` CPU node Ready, managed CNI/CoreDNS/
  kube-proxy healthy; no GPU or NAT gateway.
- Private RDS PostgreSQL 16.13 available; parameters in sync. A temporary EKS
  Job enabled pgvector 0.8.1, verified TLS 1.3 with `sslmode=verify-full`, and
  successfully computed a vector distance. Its admin secret and namespace were
  deleted. Backend migrations and application SQL users remain B1 work.
- Account-wide monthly budget USD 80 with actual/forecast alerts at USD 64.
  The recipient is in ignored local variables, not this repository.
- Bootstrap and dev state are stored under distinct encrypted S3 keys. The
  post-deployment dev plan returned no changes. Local mock tests passed 2
  bootstrap and 13 dev scenarios; the configuration scan had no unexcepted
  HIGH/CRITICAL findings. This does not verify C2 image delivery or C3 workloads.

C3 application deployment, ALB ingress, KEDA and monitoring have not been applied.
The latest backend contracts also require C3 to reconcile the original Phase 1
queue/topic and IRSA baseline below with [team alignment](../../docs/team-alignment.md)
and the [Matching runtime contract](../../services/matching-service/README.md):
additional catalog/result events, durable PostgreSQL task scaling, Cognito V2
claims/M2M scopes, signed POST upload CORS, and per-service SQL credentials are
not yet wired by this stack. Provisioned resources do not mean the application
is ready to accept real user traffic.
The current checkout's verified kubectl 1.36.5 and isolated connection config are
in ignored `.terraform/tools/` and `.terraform/validation/kubeconfig`. The system
Docker kubectl was 1.29; use a client within one minor version of the cluster.
Resources are now billable; the short-window cleanup guidance below matters.

## Defaults and deployment inputs

The examples use the team account `152174417545` and region `ap-southeast-1`.
Both provider and remote state restrict access to the configured account.
Do not use the C2 ECR publisher role for infrastructure provisioning. Use an
authenticated `lostlink-dev` profile with permissions for the planned resources,
IAM role creation/PassRole, KMS grants, Secrets Manager and EKS access entries.

Before a live plan, supply:

- Your stable IAM role/user ARN for `eks_admin_principal_arns`. For an STS
  assumed-role session, obtain its underlying IAM role ARN, including any role
  path. STS session ARNs and the account root are rejected.
- Your actual public egress IPv4 CIDR (normally `/32`) for the EKS API allowlist.
- Exact frontend origins and Cognito callback/logout URLs. The sample allows
  localhost ports 5173 and 5174; update these when the frontend gets a dev URL.
- The team's approved monthly USD budget and real alert email addresses. The
  sample uses zero deliberately, which validation rejects until you set a real
  budget; this input is an alert threshold, not a cost estimate or spending cap.
- Optionally an SES sender email; its owner must complete the verification email.

Review sizing and availability before applying:

- Kubernetes defaults to 1.36; confirm its regional availability and supported
  addon/AMI versions. Set `addon_versions` after testing the desired versions.
- One on-demand `t3.large` CPU node (8 GiB) is the student demo starting point.
  The managed group has min/desired/max of 1 by default; it has no GPU and no
  installed cluster autoscaler. Run one replica per service and one embedding
  job at a time initially. Measure CLIP plus monitoring memory before rehearsing;
  this sizing has not yet been proven against the complete application.
- `node_network_mode = "public"` places nodes in the public subnets with public
  IPv4 addresses for outbound HTTPS and creates zero NAT gateways. The node SG
  admits only peer-node and control-plane traffic; it has no public ingress or
  SSH rule. RDS stays in isolated private subnets. EKS uses a private endpoint
  for nodes and an allowlisted public endpoint for operators. Public subnets do
  not expose application ports by themselves; C3 must configure ALB ingress.
- The VPC/control plane still spans two AZs, but a single worker is not highly
  available. Pod scaling via KEDA is bounded by that worker's spare capacity.
  Increase `node_max_size` and then `node_desired_size` deliberately if needed;
  KEDA does not provision EC2 nodes. EKS updates may temporarily add capacity.
- To restore private workers, set `node_network_mode = "private"`. That creates
  one NAT gateway; `single_nat_gateway = false` creates two. Changing the node
  subnet mode can replace the node group. Inspect the plan before applying.
- EKS, EC2, RDS, NAT, KMS and storage are chargeable. Budget alerts report actual
  and forecast cost at 80% of the approved account-wide monthly budget. They do
  not automatically stop spending.

For a roughly five-day demo window, the current planning allowance is USD 80,
not USD 80 per full month of continuous runtime. At this alert budget, actual
or forecast cost over USD 64 triggers email; a forecast warning can arrive early
because it extrapolates a short deployment over the rest of the month. No
automatic shutdown is configured. Agree on cleanup immediately after the demo.
The rough full-month allowance is USD 220-260 including planned C3 components,
not a guaranteed bill; credits, taxes, logs, CPU credits and traffic can change it.
Bootstrap state, KMS keys, retained snapshots and stored data can continue costing
money after worker or cluster removal.

## 1. Check the local identity

Install AWS CLI v2, Terraform >= 1.10 and kubectl. Configure your login separately,
then in PowerShell:

```powershell
$env:AWS_PROFILE = "lostlink-dev"
$env:AWS_REGION = "ap-southeast-1"
$env:AWS_DEFAULT_REGION = "ap-southeast-1"
aws sts get-caller-identity
```

Check the returned account before proceeding. Do not put access keys, session
tokens or database passwords in Terraform files, backend config or GitHub.
The RDS master password is generated and managed by RDS in Secrets Manager;
Terraform outputs its secret ARN, not its value.

## 2. Bootstrap remote state

Run from the repository root:

```powershell
Copy-Item infra/terraform/bootstrap/terraform.tfvars.example infra/terraform/bootstrap/terraform.tfvars
# Review/edit the copied account and region.
terraform -chdir=infra/terraform/bootstrap init
terraform -chdir=infra/terraform/bootstrap plan -out=bootstrap.tfplan
# Review the resource list, then apply the exact saved plan.
terraform -chdir=infra/terraform/bootstrap apply bootstrap.tfplan
```

The first run uses local state because the remote bucket does not exist yet.
Preserve that state until it has been migrated. The bucket and KMS key have
`prevent_destroy`; keep them beyond the lifetime of any one dev environment.

Generate backend configuration as UTF-8 (avoid PowerShell 5's UTF-16 redirection):

```powershell
$devBackend = terraform -chdir=infra/terraform/bootstrap output -raw dev_backend_config
if ($LASTEXITCODE -ne 0) { throw "Bootstrap output failed" }
$utf8 = New-Object System.Text.UTF8Encoding $false
[IO.File]::WriteAllText((Join-Path $PWD 'infra/terraform/environments/dev/dev.s3.tfbackend'), ($devBackend -join "`n"), $utf8)

# Move bootstrap's own state to a DIFFERENT key in the same bucket.
$bootstrapBackend = ($devBackend -join "`n").Replace('lostlink/dev/terraform.tfstate', 'lostlink/bootstrap/terraform.tfstate')
[IO.File]::WriteAllText((Join-Path $PWD 'infra/terraform/bootstrap/bootstrap.s3.tfbackend'), $bootstrapBackend, $utf8)
Copy-Item infra/terraform/bootstrap/remote-backend.tf.example infra/terraform/bootstrap/remote-backend.tf
terraform -chdir=infra/terraform/bootstrap init -migrate-state -backend-config=bootstrap.s3.tfbackend
```

Confirm the migration prompt. Keep both backend configs available to authorized
operators. In a fresh checkout, copy `remote-backend.tf.example` again and run
`init -backend-config=bootstrap.s3.tfbackend` to reconnect to existing bootstrap
state; do not apply an empty local state against an existing bucket.
The generated backend/variable/plan/state files are ignored by Git. Commit the
provider lock files; they contain checksums rather than credentials.

## 3. Configure dev and adopt existing ECR

```powershell
Copy-Item infra/terraform/environments/dev/terraform.tfvars.example infra/terraform/environments/dev/terraform.tfvars
# Replace every illustrative value, especially the public IP, admin ARN,
# budget and alert email. Keep all existing C2 resources in the same account.
terraform -chdir=infra/terraform/environments/dev init -backend-config=dev.s3.tfbackend
```

If `lostlink/service-template` already exists (the manual C2 setup), copy
`imports.tf.example` to `imports.tf` before planning. Add similar import blocks
for any other already-existing repository. ECR uses AES256 and mutable tags to
match the C2 bootstrap and retry behavior. Imports do not exempt a resource from
configuration changes: inspect any replacement or deletion, and align existing
settings before applying. `force_delete = false` preserves nonempty repositories.

The existing GitHub OIDC provider and `lostlink-github-ecr-push` role remain
managed by the C2 bootstrap. This stack does not recreate, import or widen them.
Its EKS OIDC provider is separate and used only by Kubernetes workloads.

```powershell
terraform -chdir=infra/terraform/environments/dev plan -out=dev.tfplan
# Verify account, region, imports, replacements, access and expected charges.
terraform -chdir=infra/terraform/environments/dev apply dev.tfplan
terraform -chdir=infra/terraform/environments/dev output
```

The VPC depends on the budget resource so cost alerts are configured before
compute resources are provisioned. A successful apply is still followed by the
runtime checks below.

## 4. Runtime handoff and checks

```powershell
aws eks update-kubeconfig --region ap-southeast-1 --name lostlink-dev --profile lostlink-dev
kubectl get nodes
kubectl get pods -n kube-system
```

The public API accepts only the configured admin CIDRs, and nodes use the private
endpoint. A changed home/VPN public IP requires updating the allowlist. Access
entries grant cluster administration only to the explicitly listed IAM identities.

RDS is private and requires TLS. Run `enable-pgvector.sql` once with an RDS admin
identity from EKS (normally C3's migration Job), or an approved private-network
connection. Creating RDS alone does not enable an extension in the application
database. Check that `SELECT extversion FROM pg_extension WHERE extname='vector'`
returns a version. Backend owners handle Alembic migrations, service SQL users,
schemas, tenant RLS and service-specific grants. Do not give application pods the
RDS master secret. Use the RDS CA bundle with `sslmode=verify-full`.

Use Terraform outputs to configure C3:

| Output | Consumer / contract |
| --- | --- |
| `cluster_name`, `region`, `vpc_id`, `public_subnet_ids`, `private_subnet_ids`, `node_security_group_id` | Cluster and ingress setup; C3 configures ALB-to-backend ingress |
| `database` | Private connection and migration secret ARN |
| `media_bucket` | All item images use keys under `items/`; presigned PUTs must preserve Content-Type and expire in application code |
| `ecr_repositories` | C2 image destinations; Helm selects commit SHA tags |
| `queue_urls` | item-service sends embedding jobs directly; workers must extend visibility for long jobs and delete only after successful processing |
| `event_topic_arns` | JSON domain events; `item.embedded` goes to matching, `match.found` to notifications |
| `cognito` | Browser uses authorization code + PKCE; backend validates issuer, audience/client and tenancy |
| `workload_role_arns` | Annotate each Helm service account with its matching role |

Workload IRSA trusts exactly `system:serviceaccount:lostlink:<service-name>` and
audience `sts.amazonaws.com`. C3 must create namespace `lostlink`, matching service
account names and `eks.amazonaws.com/role-arn` annotations. IAM permissions are
scoped per workload. No application workload can read the RDS master secret.
Nodes require IMDSv2 with hop limit 1; ordinary pods use IRSA, not the node role.

SNS subscriptions use raw delivery: the SQS body is the published application
JSON, not an SNS envelope. Each queue has a DLQ after five receives; DLQs retain
messages for 14 days. Consumers own idempotency. Confirm publish/consume round
trips using the actual backend event schema; the scaffold has no event schema yet.

SES is optional in this infrastructure input, but email notifications need it:
set `notification_sender_email`, complete its verification, and verify test
recipients while SES is in the sandbox. IAM/SES provisioning alone cannot
complete email verification or guarantee delivery.

ALB/Helm, metrics-server, KEDA, Prometheus/Grafana and the CI deployment role are
C3. Redis, API Gateway/WAF and partner delivery are broader architecture work;
they are not silently provisioned by this Phase 1 C1 stack. Confirm their
necessity for the demo with the backend/architecture owners.

## Local checks without AWS credentials

```powershell
terraform fmt -check -recursive infra/terraform
terraform -chdir=infra/terraform/bootstrap init -backend=false -input=false
terraform -chdir=infra/terraform/bootstrap validate
terraform -chdir=infra/terraform/bootstrap test
terraform -chdir=infra/terraform/environments/dev init -backend=false -input=false
terraform -chdir=infra/terraform/environments/dev validate
terraform -chdir=infra/terraform/environments/dev test
```

Provider downloads need internet access. `terraform test` uses only mock AWS
providers and `command = plan`; no cloud resources or real backend are used.
If a checkout is already initialized against a live backend, use a separate
checkout or fresh `TF_DATA_DIR` for local validation. The `Terraform` Actions
workflow runs these checks without AWS credentials or OIDC permission.

### Scoped scanner exceptions

Five resource-local Trivy exceptions document Phase 1 decisions:

- `AVD-AWS-0040` on the EKS cluster: a public management endpoint is needed for
  local operators, but its IPv4 CIDRs are explicit, validated and cannot be
  world-open. Nodes use the private endpoint. Move to private-only access when
  the team has VPN/SSM connectivity; remove the exception at that point.
- `AVD-AWS-0041` on `public_access_cidrs`: the same deliberate operator access
  uses an exact public IPv4 allowlist (currently one /32). The embedded check
  flags any public IP, even that allowlisted address. Variable validation rejects
  CIDRs wider than /24; do not replace this with unrestricted access. Remove the
  exception along with public API access when private operator connectivity exists.
- `AVD-AWS-0031` on ECR repositories: C2 retries publish the same commit tag, and
  the existing repository is mutable. Retain this behavior until C2 handles
  immutable-tag retries; then switch and remove the exception. This does not
  expand the main-only OIDC publisher role.
- `AVD-AWS-0104` on the dedicated TCP/443 egress rule: ECR, AWS APIs and CLIP
  registries use changing public addresses. Only external HTTPS is allowed;
  other node egress is restricted to the VPC. Remove this rule/exception if all
  necessary dependencies move behind endpoints or an explicit egress proxy.
- `AVD-AWS-0164` on public subnets: the demo's public-node mode needs automatic
  public IPv4 assignment to reach AWS APIs/ECR without a NAT gateway. Node SGs
  have no public ingress, and RDS remains private. Private-node mode disables
  this assignment and uses NAT. Tests exercise both networking paths.

No security scan is globally disabled. The node security group has only VPC
egress and external TCP/443; it does not inherit the EKS primary group's broad
egress. Tests reject broad API CIDRs and check these egress rules.

## Cleanup

Application identities, credential bootstrap and V2 token-trigger instructions
are in [APPLICATION.md](APPLICATION.md). C3 uses that document's process-specific
roles and domain-event queues rather than the legacy C1 baseline outputs.

Review a destroy plan before removing dev. First delete C3 ingress/load balancers
and Kubernetes resources that create AWS dependencies. RDS deletion protection
and Cognito deletion protection must be deliberately disabled and applied before
destroying; RDS also creates a final snapshot named `lostlink-dev-final`. If that
snapshot already exists, choose a new final snapshot identifier in code before
cleanup. ECR and media buckets refuse deletion while nonempty. Retain data or
empty it deliberately; the stack does not force-delete it. Bootstrap's bucket
and key are protected separately and should remain intact with both state keys.
