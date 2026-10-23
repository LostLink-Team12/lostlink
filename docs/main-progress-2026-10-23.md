# main 进度与部署方式核对（2026-10-23）

本文核对 #20、#22、#23、#24 的代码与合并后的主线职责，不把仓库合并、镜像发布、云端部署或验收视为同一件事。本次操作修复并合并 PR，不执行 AWS 部署。

## 1. 主线已有能力

| 范围 | 已有能力 | 仍需完成或确认 |
| --- | --- | --- |
| Seed 1 后端 | 共享身份、租户隔离、UC1 场馆、UC2 登记与照片、UC3 CLIP 检索/报告、UC4 通知、UC5 审核与 QR 交接，持久化任务及 inbox/outbox | 以当前主线镜像重新完成云端联调；实体相机、真实收件与人工表单验收仍按 C3 记录核对 |
| 匹配标定 | #20 统一 JSON/Markdown 标定产物，真实固定模型工作流可运行；#22 默认照片 0.78、纯文字 0.63，两条持久化匹配路径使用统一选择方法 | 数据仍包含重复场景、同图正例且无独立验证集；纯文字召回有限，不能宣称生产质量已验收 |
| Seed 2 Delivery | #23 提供持久化运单、失主指定伙伴的交付授权、加密地址、伙伴状态接口、Claim 事件消费、历史与 outbox、RLS 和幂等；柜台自取会取消待取件运单并停止伙伴地址读取 | 当前 C3 部署脚本只列出五个 Seed 1 服务；Delivery 的数据库/秘密、IAM、事件队列与订阅、伙伴 scope/client、chart 和入口路由仍需接入并完成云端演示；完整 Partner Webhook 平台没有实现 |
| 云原生基础 | Terraform、EKS、RDS/pgvector、S3、SNS/SQS/DLQ、Cognito、SES、Secrets Manager、IRSA；HPA/KEDA、Prometheus/Grafana | 不等于全部性能目标通过；节点容量目前由操作员调整，尚无节点自动伸缩器 |
| CI/安全 | 单元及真实容器集成、契约检查、Ruff、Terraform/Helm 校验、Trivy、镜像构建与固定 CLIP 推理验证 | 交付新运行版本后仍需针对该版本验收 |

#23 细节见 [柜台领取修复说明](reviews/2026-10-23-pr23-counter-collection.md)；#20 细节见 [标定修复说明](reviews/2026-10-23-pr20-calibration.md)。历史计划中的“Delivery 仍为骨架”“标定工作流待合入”不再代表本次主线状态。

## 2. 已有自动发布，但没有自动部署到 EKS

`.github/workflows/ci.yml` 会在 PR 上运行验证，在 main push 上验证、构建和扫描镜像；全部镜像通过后，通过 GitHub OIDC 推送同一批已扫描镜像到 ECR，标签为该次 main 提交的完整 SHA。本次只读检查确认三个 AWS 发布变量已配置，main 的既有 CI 运行 39929718738 中七个 publish-image 任务均成功。

这条流程止于 ECR，没有 `helm upgrade`、应用迁移、EKS rollout、部署后冒烟或失败回滚步骤。`c3.yml` 只验证部署契约；Terraform 工作流只进行格式、validate 和 mock test；Cognito 工作流构建和验证 ZIP，也不负责更新云端函数。因此目前有 CI 和自动镜像发布，尚无完整的合并后自动云端 CD。

## 3. 当前部署如何执行

1. 操作员准备 AWS profile、工具、受限 EKS 访问及 Terraform 参数，检查计划后 apply 基础设施。
2. `infra/deploy/configure.py` 与 `controllers.ps1` 准备配置、内部 TLS、控制器和监控权限。
3. `infra/deploy/build.py --tag <完整提交 SHA>` 从本地工作目录构建五个后端及前端镜像并推送 ECR，记录镜像 digest 与构建来源；Matching 复用固定 digest 的 ML 依赖层并拷贝当前应用及共享库代码。也可选择 CI 已发布的后端镜像，但仍需明确版本并接入部署清单。
4. 使用受隔离的迁移凭据运行 bootstrap/migration Job；运行值只注入各进程授权访问的秘密 ARN。
5. 生成 Helm values，手动执行 `infra/deploy/releases.ps1` 更新五个独立服务 release 与 platform release，再检查 readiness、rollout、HTTPS 冒烟与业务验收。
6. 对公网使用 CloudFront HTTPS 和私有 ALB，后者进入前端 gateway；内部接口与监控不经公网入口暴露。

仓库同时保留 `infra/helm/lostlink` umbrella chart。C3 已记录的实际方案使用五个服务 chart 加 platform，两种方案不可装入同一 namespace 争用服务身份。Deployment 使用固定镜像 digest，因此 ECR 出现新标签不会自行触发更新。

## 4. 部署是否始终以 main 为准

**目前没有强制保证。** main 是审阅合并后的代码基线，CI 发布镜像确实来自 main 的相应提交，但操作员部署的直接输入是选定的镜像 digest、Helm chart 和 values。

本地 build 脚本从执行时的目录构建，只记录 HEAD 与已跟踪工作差异的 SHA256；它没有强制检验分支为 main、工作目录干净、HEAD 等于最新 origin/main 或排除未跟踪构建输入。release 脚本读取本地 values 中的 digest。因此能够部署旧提交、其他分支或本地未提交版本。

仓库 C3 证据 `reviews/c3-evidence/image-digests.json` 记录的部署来源为 `62c0f4d93466d31763a4d1493a8dcfa324ba3daf`，早于本次 PR 合并；这是一份历史部署证据，不能当作实时集群盘点。本次环境没有现成的 C3 kubeconfig/工具，未重新核对线上所有 Pod，也未把 #20/#23 自动部署到云端。

若要声明“线上等于 main 的某个已批准版本”，下一步需要：为干净的 main 提交建立统一发布 manifest（提交、各镜像 digest、chart/values 版本）；迁移和 Helm 使用同一 manifest；部署后采集实际 imageID/就绪状态及验收记录。若进一步要求自动 CD，应把这些步骤接入受保护的 GitHub environment，配置 OIDC 最小部署权限、发布串行锁、失败回滚和部署记录。合并动作本身不代表部署完成。

## 5. 当前主要缺口

- 自动云端部署与版本一致性约束。
- Delivery 在 AWS、路由和前端演示中的接入。
- 已有 HPA 扩容证据，但 C3 200 VU 搜索最后一轮 p95 约 11.85 秒，未达 5 秒门槛，严格有效请求率也未全通过；不能把“扩容发生”写成“性能验收通过”。
- 剩余真实邮件、相机、表单人工验收，以及 Seed 2 工时对比、报告和演示材料。
