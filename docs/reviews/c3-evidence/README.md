# C3 evidence

See [the deployment record](../2026-10-23-c3-deployment.md) for outcomes and limits.
Times in run metadata, collectors and screenshots use UTC; the record uses
Asia/Shanghai dates. These are observations from dev, not production guarantees.

- `*-summary.json`: actual k6 results, including failed thresholds. Bulk HTTP
  latency is upload/import latency, not search latency.
- `*-run.json`: run identity, completion, cooldown/drain and historical failures.
  The resumed bulk run reuses 4,953 photos and uploads 47; it is not a fresh
  5,000-photo scaling demonstration.
- `*-scaling.csv`: observed desired/ready replicas and Prometheus aggregates.
  `*-controllers.json` retains HPA/KEDA conditions, CPU measurements where
  available, and collection errors. Sanitized `*-events.json` retains Kubernetes
  events. Missing data is not zero. ScalingLimited at an intentional replica cap
  is retained rather than hidden.
- `search-v*-profile.json`: actual nodes, image digests, resource reservations,
  model placement and HPA configuration. The older five-node v2/v3 runs used
  2500 MiB requests and must not be reused as a large-photo capacity guarantee.
  Current charts enforce the 4 GiB model reservation from #17.
- `image-digests.json`: latest deployed build source/digests. Earlier manifests
  are kept separately. `matching-memory.json` explicitly references the E75
  manifest used in its isolated 16 MP test; RSS is not cgroup total memory.
- `business-acceptance.json`, `staff-acceptance.json`: real API checks through
  Cognito, AWS events and deployed services. Synthetic invitations/email delivery
  were suppressed; these do not establish physical camera or mailbox acceptance.
- `photo-repairs.json`: original/derived hashes and versioned API repairs for six
  unsupported D1 originals. Seven failed historical jobs remain as audit records.
- `grafana-*.png`: genuine dashboard screenshots, separate from generated load
  HTML plots. Monitoring is private and uses temporary emptyDir storage.

Secret-bearing Helm values, Terraform state/plans, credentials, access tokens,
presigned fields and the original photo archive are intentionally excluded.
