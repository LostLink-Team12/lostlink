# Infrastructure (owner: LU JINGXING)

- `terraform/` - AWS: VPC (2 AZ), EKS, ECR, RDS PostgreSQL + pgvector, S3, SQS, SNS, ElastiCache, Cognito, SES, WAF. Remote state in S3 with locking.
- `helm/` - one chart per service (Deployment, Service, HPA or KEDA ScaledObject, probes)
- Cost control: smallest instance sizes, Spot for worker nodes, `terraform destroy` when idle, AWS Budgets alarm.
