# Existing Trivy exceptions

Reviewed against source on 2026-11-04. These six existing resource-local ignores
are preserved; this change adds no vulnerability or secret exceptions. Next
manual review: **2026-12-04**, or before using this configuration for production.
This is a review date, not an automatically enforced scanner expiry.

| Rule | Source | Existing rationale and boundary |
| --- | --- | --- |
| AVD-AWS-0011 | `infra/terraform/environments/dev/c3.tf` | The dev CloudFront distribution defers paid WAF. Authentication and the private restricted origin remain; production needs its own reviewed WAF policy. |
| AVD-AWS-0040 | `infra/terraform/environments/dev/eks.tf` | A public EKS management endpoint supports explicit operator/runner addresses. Private node access remains enabled; the world must never be allowlisted. |
| AVD-AWS-0041 | `infra/terraform/environments/dev/eks.tf` | The public CIDR check also flags the deliberately narrow operator allowlist; existing variable validation enforces that scope. |
| AVD-AWS-0164 | `infra/terraform/environments/dev/network.tf` | Public node addresses allow demo egress without a NAT; node security groups do not grant public inbound application access. |
| AVD-AWS-0104 | `infra/terraform/environments/dev/network.tf` | The dedicated TCP/443 egress rule reaches AWS/ECR/model registries with changing public addresses. It is not an all-protocol egress exception. |
| AVD-AWS-0031 | `infra/terraform/environments/dev/storage.tf` | Existing backend ECR SHA-tag retries require mutable tags; main-only OIDC limits pushing. Frontend immutable-tag retry behavior is separate. |

Review each original resource/comment before changing or removing an ignore.
The source configuration, IAM scope and existing deployment tests remain the
authority for its boundary. Scanner reports still retain all non-ignored
severities and unfixed dependency vulnerabilities for triage.
