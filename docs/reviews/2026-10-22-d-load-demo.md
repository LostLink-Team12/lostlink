# D 压测与扩容演示交付状态（Issue #10）

日期：2026-10-22。D 的代码在 `feat/d-load-scaling-demo`；C3（Issue #6）是实测依赖。

## 已完成的脚本

- `tests/load/search_load.js`：默认 0 → 25 → 50 → 100 → 200 → 0 VU；检查响应契约、错误率和 p95。
- `tests/load/bulk_import.js`：默认 5,000 张 D1 图片、10 VU、50 条/批；稳定幂等键、S3 表单上传、版本校验、完成轮询、续跑跳过已确认物品。
- `prepare-load-data.mjs`：逐张校验 SHA256 并生成按需读取的照片包，避免每个 VU 复制完整数据集。
- `capture-scaling.mjs`：只读采集 Deployment、HPA、KEDA 条件和真实 Prometheus 指标，导出 JSON/CSV/HTML。缺失数据明确记为不可用。
- `render-results.mjs`：汇总关键数字与图表页面，供真实运行后导出截图。

运行参数、令牌要求、续跑规则和截图流程见 `tests/load/README.md`。

## 已检查的依赖分支

已于本次工作中 fetch origin，再检查以下提交：

| 分支 | 提交 | 可确认的内容 | 不包含的 C3 内容 |
| --- | --- | --- | --- |
| `origin/codex/terraform-dev` | `2808713` | Terraform 基础设施；README 记录 2026-10-22 EKS/RDS 部署验证 | 明确未应用 Helm/ALB、KEDA 和监控 |
| `origin/codex/app-aws-config` | `742b27b` | 继承基础设施，增加应用队列、进程身份、数据库引导与 Cognito；APPLICATION.md 记录真实迁移、IAM、M2M、事件路由验证 | 明确不安装 Helm、Ingress、权重或 CD；真实用户登录及完整应用就绪仍归 C3 |

上表是分支代码与其验证记录的审查，不是本次重新登录 AWS 后的资源现状证明。
`app-aws-config` 的 APPLICATION.md 还明确要求修复 Claim outbox 和 Notification
consume 的分模式启动校验；不得用虚假配置或过度授权绕过。

仓库中这两个分支均无应用 Helm chart、HPA/ScaledObject、监控部署或可运行的应用 dev URL。
因此不能因为 AWS 底座已部署，就把 C3 或 D 的云端验收标记为完成。

## 当前验证与未完成验收

已使用校验官方 SHA256 的 k6 1.6.0 在本地契约替身上运行实际脚本，验证检索加压、
带版本的上传申请、S3 multipart 凭据隔离、pending 轮询、同一 RUN_ID 去重续跑和失败退出。
本地准备了完整 5,000 张 D1 照片包并校验其 SHA256；这不代表已向 dev 导入。

待 C3 提供部署入口、有效测试身份、标签、监控地址和真实 backlog 指标后执行：

- 在 dev 运行 200 VU 检索，记录 HPA 扩容、p95 和错误率。
- 在 dev 导入 5,000 张照片，观察 PostgreSQL runnable embed backlog 与 matching tasks 的 KEDA 扩缩容。
- 在 cooldown 和 backlog drain 后导出真实曲线截图及关键数字，记录可检索完成情况。

目前没有云端扩容截图、实测吞吐/p95 或真实 5,000 图导入结果；不得以本地替身数据冒充。
