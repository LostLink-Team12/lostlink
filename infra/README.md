# Infrastructure (owner: LU JINGXING)

- `terraform/` - AWS: VPC (2 AZ), EKS, ECR, RDS PostgreSQL + pgvector, S3, SQS, SNS, ElastiCache, Cognito, SES, WAF. Remote state in S3 with locking.
- `helm/` - one `lostlink` chart for all services and workers (Deployments, Services, IRSA service accounts, HPA, KEDA ScaledObject, internal TLS). See [helm/README.md](helm/README.md).
- Cost control: smallest instance sizes, Spot for worker nodes, `terraform destroy` when idle, AWS Budgets alarm.
- `ci/` - GitHub OIDC trust and ECR push policy templates. See [C2 setup and acceptance checks](../docs/ci.md).
