# dev 自动 CD 实施记录

`main` push / 在 `main` 手动运行 CI → 测试、构建、扫描 → ECR 发布（包括前端）→
`Deploy dev EKS`。PR 只执行验证；CI/发布失败时不部署。队列串行，不取消正在部署的任务；
开始前发现 main 已更新则跳过旧版本。

CD 只修改现有五服务及前端、积压 exporter 的镜像和运行脚本修订，共 20 个 Deployment。
HPA/KEDA、资源、环境、身份与 TLS 配置继续由操作员维护；Delivery/合作方不是本轮 C3 的部署目标。
迁移源码变化会在修改工作负载前失败，由操作员完成迁移及发布后重新审批基线。

## 实际完成

- Terraform 计划及 apply：仅新增 3 项——main-only GitHub OIDC 部署角色、资源限定策略、
  EKS access entry；0 项现有资源修改、0 项销毁。
- 已设置 `AWS_DEPLOY_ROLE_ARN`，与现有镜像发布角色分离。
- 已按实测 `62c0f4d` 镜像清单校验现有 API，初始化迁移 hash/HTTPS origin 基线。
- namespace RBAC：仅允许 patch 指定 20 个 Deployment、`lostlink-runtime` 和
  `lostlink-cd-state`；只读 Deployment/Pod 用于 rollout。
- 真实 impersonation：patch `item-api` 和 CD state 允许；读测试 Secret、创建 Job、
  patch Grafana 均拒绝。服务器 dry-run 成功，未修改工作负载。
- 当前源码迁移 hash 与已部署基线一致；20 个实际目标及容器名均校验通过。
- 本地部署/CD 检查 23 passed（含新增 13 项），Terraform mock 16 passed；
  前端 28 tests 和生产构建通过；actionlint 1.7.12、Ruff、diff 检查通过。
- AWS Access Analyzer 校验已应用身份策略，findings 为空。
- 新增前端镜像扫描识别旧 Nginx/Alpine 镜像的已修复高危漏洞；升级到官方
  1.30.6 alpine-slim，并固定 linux-amd64 digest，没有降低扫描门槛。

## 权限与故障边界

最初 Helm 权限方案被自动审批拒绝，原因是持久化 Secrets/RBAC 权限过宽。
最终使用指定资源 patch 方案，不授予这些权限。SDK IAM 基线由 IAM Policy Autopilot
0.3.0 生成，并收窄到实际 dev cluster/ECR 仓库；移除了基线中不适用于 CIDR 更新的
CreateAccessEntry/PassRole 扩展。

GitHub runner 临时添加自己的 `/32`，结束时以 `always()` 清理，保留其他 CIDR。
强制丢失 runner 仍可能阻止清理；操作员按日志中的精确 `/32` 清理，不整体覆盖 allowlist。
工作负载/HTTPS 验证失败会还原旧 runtime 与镜像/修订并等待 rollout；发布和回滚各有
15 分钟共享等待预算，失败仍报告失败；
回滚失败明确请求人工处理。Helm 历史镜像值可能落后于 CD，实际镜像与 CD state 才是发布记录。

首次 GitHub OIDC → EKS 的完整自动发布需此变更合并 main 后执行；PR 中不会为了测试而
绕过 main-only 信任条件或部署共享 dev。此前真实邮箱、实体相机及性能验收结论保持不变。

## 审阅后修复：干净 runner 的冒烟证据目录

HTTPS 冒烟在写入证据前显式创建本地输出目录及父目录，避免 Git 忽略的 `.local` 在新 runner 上不存在，导致健康发布因写文件失败而回滚。新增离线回归用例：模拟全部 HTTP 边界检查通过，保持输出目录不存在，并验证最终证据文件确实生成；测试还确认 `--origin` 路径不会读取 Terraform state。

该测试在修复前复现 FileNotFoundError，修复后通过。PR 的 publish-image 与 Deploy dev EKS 仍按原有 main-only 条件跳过；没有为测试放宽云端发布或部署条件。
