# PR #29 dev 部署的迁移恢复

PR #29 合并后的 main 为 `6b380a38fe014748a1b6a93ed3e6d18620f05568`。
[CI 首次运行](https://github.com/LostLink-Team12/lostlink/actions/runs/40093712068)
的测试、构建、扫描及发布成功，`Deploy dev EKS` 在修改工作负载前被迁移检查阻止：

```text
Database migrations changed; run operator migrations and approve the baseline
```

该 PR 新增 Claim 的 `0002_claim_report`，添加可空 `lost_report_id UUID` 列及部分索引。
线上部署记录仍为 PR #28 的 `b68aa46bd0253899eb893180d64498eece33e3bf`，
Claim 基线尚未包含此次迁移。OIDC、runner 访问租约和清理均成功，问题不是 AWS 登录
权限或镜像发布失败。迁移检查按设计执行，不能跳过该检查或只改 hash 来放行。

新增操作员入口 `infra/deploy/cd_migrate.py`，从失败运行的完整 source SHA 解析已发布镜像，
复用现有 `bootstrap-app.py` 的隔离迁移、15 个运行角色验证和临时凭据/CREATE 权限清理。
全部成功后通过 ConfigMap resourceVersion 比较并交换，只更新迁移 hash 和迁移源码记录；
已部署 source/images 保持原值，直至原 CI 真正部署成功。迁移、镜像预检或并发检查失败时
不会批准基线。GitHub Actions 的权限保持原样，未授予数据库迁移或 Secret 读取权限。

操作员需要先恢复短期 AWS 登录，核对账号和 EKS endpoint，再执行恢复，并只重跑失败作业。
Claim 的 readiness 精确检查数据库 revision，因此迁移后至新版 rollout 完成之间可能短暂
不就绪。旧版本不是本次迁移后的通用回滚目标，不应自动 downgrade 删除新增来源数据。

本地验证：39 项部署/恢复测试通过；Ruff 检查及格式检查通过。
测试覆盖迁移失败不批准、镜像缺失不执行迁移、并发修改不批准、源码不匹配不执行，
以及成功后保留原部署记录。未放宽原迁移检查。
