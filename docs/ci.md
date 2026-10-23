# Continuous integration and ECR publishing (C2)

Owner: LU JINGXING. Workflow: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

Scans use Trivy 0.76.0 explicitly. The service template pins FastAPI 0.150.0 and
Starlette 1.6.0 because its previous FastAPI pin pulled in Starlette 0.38.6, which
failed the image gate on CVE-2024-47874, CVE-2026-48818 and CVE-2026-54283.

## Pipeline

1. Discover `services/*/Dockerfile`. Service names must use lowercase letters,
   digits and single separating hyphens; each service needs `requirements.txt`.
2. Run each service's tests on Python 3.12, shared-library tests, Ruff, and
   `python scripts/export_contracts.py --check` to reject stale API/event contracts.
   Run a repository-wide Trivy scan
   for HIGH/CRITICAL vulnerabilities with fixes available, secrets and IaC issues.
3. After both gates pass, build each service's Linux image and scan its OS and
   application dependencies with Trivy (HIGH/CRITICAL with fixes available).
   Matching is built with `INSTALL_ML=true` and must pass an offline CLIP inference
   check inside the built image before it can be scanned and published.
4. On `main`, save the scanned images as gzip-compressed artifacts, retained for
   one day. Artifact upload compression is disabled to avoid compressing twice.
5. Only after **all** service builds and scans pass, a separate publishing job
   assumes an AWS role through GitHub OIDC, downloads the images from the same
   workflow run and pushes them to ECR. It does not rebuild the images.

Pull requests run tests, scans and builds without AWS configuration or OIDC write
permission. Publishing runs on pushes to `main` and manual runs selected on
`main`. No long-lived AWS access keys are needed. Until `AWS_ROLE_ARN` is set,
the publishing job is skipped. Once enabled, missing or invalid account/region/role
configuration fails publishing with an actionable error. A green run with publishing
skipped does not prove that images were uploaded or deployed.

Images use the merge commit's full SHA:

```text
<AWS_ACCOUNT_ID>.dkr.ecr.<AWS_REGION>.amazonaws.com/lostlink/<service>:<GITHUB_SHA>
```

The published URI appears in each publishing job's summary. C3 can use this tag
for deployment. On `main`, publishing now leads to `Deploy dev EKS`; see the
[CD runbook](../infra/deploy/README.md#automatic-application-cd). A separate OIDC
identity patches existing application workloads; PR runs never deploy.
The frontend gateway is also built, tested, scanned and published by this CI run,
so its image and the backend images share the deployed source SHA.

## Matching inference image

CI publishes one inference-capable `lostlink/matching-service:<GITHUB_SHA>` image
for the API and `tasks` worker. It installs the existing pinned
`services/matching-service/requirements-ml.lock` through `INSTALL_ML=true`. The
`consume` and `outbox` processes can use the same image. The Matching build job
allows 60 minutes for the larger ML dependency installation, checks and image scan;
other service builds retain their 30-minute timeout.

The ML lock pins Pillow 13.0.0 to satisfy the image vulnerability gate; the previous
12.3.0 pin has fixable HIGH/CRITICAL findings. See the
[Pillow 13.0.0 security fixes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html#security).

The image check downloads the official checkpoint with
`scripts/prepare_clip_model.py`, verifies its pinned SHA256, and mounts it read-only
in a container running as the image's default non-root user with networking disabled.
The container loads the real `ClipEncoder` and encodes both text and a generated PNG,
checking for finite, unit-length 512-dimensional vectors. A missing ML dependency,
incompatible runtime, or failed model load blocks publishing.

Weights are not baked into the image. API and `tasks` deployments must still mount
the same trusted checkpoint read-only and configure `CLIP_CHECKPOINT` and
`CLIP_SHA256` as described in the [Matching runtime setup](../services/matching-service/README.md).
The model check does not replace `/ready` checks for database migrations, identity
configuration and service dependencies, or the AWS end-to-end acceptance checks.

## AWS setup after the account is confirmed

This setup targets the standard AWS partition (`arn:aws`), for example the
Singapore region `ap-southeast-1`. Use your own authenticated AWS profile for
setup; do not put AWS access keys in the repository or Actions secrets.

1. Create or reuse the IAM OIDC provider with URL
   `https://token.actions.githubusercontent.com` and audience `sts.amazonaws.com`.
2. Create a dedicated role, for example `lostlink-github-ecr-push`, using
   [`github-oidc-trust-policy.json`](../infra/ci/github-oidc-trust-policy.json).
   Replace `<AWS_ACCOUNT_ID>` and `<GITHUB_OIDC_SUBJECT>` before applying it.
3. Attach [`ecr-push-policy.json`](../infra/ci/ecr-push-policy.json), replacing
   `<AWS_ACCOUNT_ID>` and `<AWS_REGION>`. It allows login and image pushes only
   within `lostlink/*`; it cannot create repositories or deploy infrastructure.
4. Provision one private ECR repository per discovered service in the same account
   and region (C1/Terraform owns these): `venue-service`, `item-service`,
   `matching-service`, `claim-service`, `notification-service`, `delivery-service`
   and `service-template`, each under `lostlink/`. The template is discovered by CI
   but does not need a business deployment. For a manual bootstrap of one repository:

   ```bash
   aws ecr create-repository \
     --region <AWS_REGION> \
     --repository-name lostlink/service-template \
     --image-scanning-configuration scanOnPush=true
   ```

   The command creates a repository with the default mutable tag setting, so a
   rerun can republish a SHA tag. If C1 enables immutable tags instead, rerunning
   an already-published commit will fail on that existing tag. Configure an ECR
   lifecycle policy in C1 to bound image storage costs.
5. Under GitHub **Settings > Secrets and variables > Actions > Variables**, add:

   | Repository variable | Value |
   | --- | --- |
   | `AWS_ACCOUNT_ID` | The 12-digit AWS account ID |
   | `AWS_REGION` | Region containing the ECR repositories, e.g. `ap-southeast-1` |
   | `AWS_ROLE_ARN` | ARN of the dedicated OIDC role |

### Choose the exact OIDC subject

Restrict the role's trust to this repository's **main branch**, using
`StringEquals`. Do not use a wildcard subject.

GitHub's default subject format depends on the repository's creation date and
OIDC settings. Repositories created after 15 July 2026 (and older repositories
that opted in, were renamed or transferred) use owner and repository IDs:

```text
repo:LostLink-Team12@<OWNER_ID>/lostlink@<REPO_ID>:ref:refs/heads/main
```

Older repositories that still use the previous default format use:

```text
repo:LostLink-Team12/lostlink:ref:refs/heads/main
```

Check the repository's OIDC settings before choosing the subject. You can obtain
its creation date and IDs with:

```bash
gh api repos/LostLink-Team12/lostlink \
  --jq '{created_at: .created_at, owner_id: .owner.id, repo_id: .id}'
```

If your organization customizes OIDC subjects, use that configured format.
This workflow does not select a GitHub environment; adding one later also
changes the default subject. See the
[GitHub OIDC reference](https://docs.github.com/en/actions/reference/security/oidc)
and [AWS credentials action](https://github.com/aws-actions/configure-aws-credentials).

## Acceptance checks

- Open a PR: tests, repository scan, image builds and image scans must pass;
  publishing must be skipped, including for fork PRs.
- Require the test matrix, `security-scan` and build matrix as branch-protection
  checks for `main`, plus the one review required by `CONTRIBUTING.md`. Update
  required matrix check names when adding services.
- After the AWS setup and merge, confirm all publishing jobs succeed and verify
  the full commit-SHA tag exists in each ECR repository:

  ```bash
  aws ecr describe-images \
    --region <AWS_REGION> \
    --repository-name lostlink/service-template \
    --image-ids imageTag=<FULL_MERGE_COMMIT_SHA>
  ```

- If AWS is configured after the first run failed, rerun the failed jobs within
  the one-day artifact retention period, or start **Actions > CI > Run workflow**
  on `main` to rebuild, scan and publish. Later reruns also need a fresh manual
  run because image artifacts will have expired.
- A failing test, repository scan, build or image scan must block publishing for
  every service. Do not bypass the scan to make publishing pass; update vulnerable
  dependencies or base images in the affected service.
- New services copied from `service-template` are discovered automatically once
  they have a Dockerfile. Create their corresponding `lostlink/<service>` ECR
  repository before merging.
