# C3 dev 部署与真实验收记录

记录日期：2026-10-22 至 2026-10-23（Asia/Shanghai）。关联 #6、#10、#15、#16。

## 已部署的入口与进程

- [Owner App](https://d37mn7oo1kv0ok.cloudfront.net/owner-app/)
- [Venue Portal](https://d37mn7oo1kv0ok.cloudfront.net/venue-portal/)
- 集群/context `lostlink-dev`，namespace `lostlink`；工作负载标签
  `app.kubernetes.io/part-of=lostlink`。
- 五个服务各自 chart：API、独立 workers、迁移 Job、进程级 IRSA、资源配额和探针。
  15 个运行时数据库角色分别验证 TLS、schema 隔离、非 owner、无 DDL、无 BYPASSRLS。
- API 使用 CPU HPA；Matching tasks 使用 ADR-0002 的 PostgreSQL runnable embed SQL。
  监控角色仅可 SELECT 工作表的三个积压相关字段，不能读取任务载荷或修改工作记录。
- Matching API/tasks 使用同一份固定 SHA256 的真实 CLIP 权重；应用镜像按 digest 部署。
- CloudFront HTTPS → VPC origin → 私有 ALB → frontend gateway → TLS ClusterIP API。
  公网不暴露 `/internal/v1/*`、`/metrics`、OpenAPI 或数据库。
  `/matches/*` 保留原始 URL 并回落到邮件链接页面；S3 使用直接 presigned POST。

操作与版本见 [部署 runbook](../../infra/deploy/README.md)。镜像、业务、压测与截图证据
放在本记录相邻的 `c3-evidence/`。未导出 Terraform state、口令、访问令牌或 presigned fields。

## 实际完成的业务验证

使用六个隔离的 synthetic Cognito 账号，真实授权码 + PKCE 登录，没有邀请邮件。
密码保留在 Kubernetes Secret。测试账号的受信任 email_verified 标志不代表邮箱验证流程已验收。

| 环节 | 真实结果 |
| --- | --- |
| UC1 场馆/员工 | 两个场馆入驻；跨场馆 Item 404；员工新增、改角色、暂停后旧 token 403、重新激活通过 |
| UC2 物品 | presigned S3 文件最后一个表单字段；确认、封存、SNS/SQS、CLIP、最终 available 通过 |
| UC3 报告/搜索 | 带照片报告、真实向量检索、匹配记录和真实库存搜索通过 |
| UC4 通知 | Notification inbox 观察到真实匹配；发送器暂停时测试，关闭 synthetic 报告后恢复 |
| UC5 认领/交接 | 未验证失主 409、过期版本 412、审批异步完成、原始 QR payload 交接，Claim/Item 均 handed_over |
| 浏览器 | Owner App 与 Venue Portal 均通过真实 Cognito 登录；场馆页面显示真实库存与 handed_over 记录 |
| 公网边界 | 15 项 HTTPS、路由与私有接口拒绝检查通过 |

没有向其他人发送消息或邮件。真实邮箱收件、完整浏览器逐步操作及实体摄像头扫码仍需人工验收；
本次交接验证使用原始 QR payload，并不声称实体摄像头已通过。

## 批量导入和恢复

使用 D1 的 5,000 张原始照片包，逐张 SHA256 校验，包大小 1,517,351,681 bytes。
初次真实运行 `c3-bulk-20261022` 以 10 VU、50 条/批执行，4,953 张确认成功；一次 S3 EOF
打断了一批，因此 k6 严格阈值失败。81 次指标采集没有缺失，真实 runnable backlog 触发
Matching tasks 从 1 扩到 2。该失败结果保留，不能作为一次全新 5,000 张全成功运行。

发现六个源文件不符合模型约束：五个多帧 MPO、两个超出字节/像素限制的源图，其中一个重叠。
通过 Item 的带版本 API 为这六件物品上传单帧、最长边 2048 的 JPEG 衍生图；更新修改说明，
保留原图、来源授权及原始/衍生 SHA256。修改说明曾触发一次旧照片 embedding，故保留七条
历史 invalid_image 审计，均不属于最终未完成物品。没有删除或篡改失败任务来制造零积压。

以相同 RUN_ID、manifest、batch size 续跑，证据目录 `c3-bulk-20261022-resume`：
确认 5,000，复用 4,953，实际补传 47；k6/collector 均退出 0。历史 failed 数 7 → 7，
runnable/leased/delayed 均排空，五个 API 和 Matching tasks 回到各 1 并稳定 180 秒。
最后逐页核对 Item：**5,000 available、1 handed_over**，真实 owner 搜索非空。

因此最终 5,000 件可搜索已验证；续跑不是第二次全新 5,000 张扩容演示。最初 bulk 同时运行
每 30 秒一次真实 owner 搜索 canary，其延迟不能冒充 200 VU 压测结果。

## 搜索与扩容

首次两节点、最多两个 API 副本的默认 13 分钟 0 → 25 → 50 → 100 → 200 → 0 VU 运行：
9,440 请求，12.073 req/s，p95 19,255.996 ms，16 次超时。严格响应契约和 5 秒 p95 目标失败。
44 次采集无缺失，API 1 → 2 → 1，冷却已观察。该轮当时没有完整 5,000 件库存。

修复了同步推理挤占线程池时阻塞探针的问题：health/metrics 异步，ready 使用独立执行器，
已加载模型的 ready 不再等待推理锁。实际运行还修复了 SQS 长轮询的过短 read timeout，
并按进程拆开 Claim outbox/Notification consume 启动校验，没有注入假凭据。

复测使用临时五节点容量、Matching API 上限八副本，且在完整 5,000 件库存就绪后开始。
复测结果、同时间范围 Grafana 截图及最终两节点恢复状态将在验收结束后填入下表。

| 项目 | 结果 |
| --- | --- |
| 200 VU 复测 | 运行中，尚未判定通过 |
| 最终节点容量 | 待恢复两节点并复核 |

KEDA/HPA 不提供节点容量；本次节点容量由操作员通过 Terraform 调整，没有安装节点自动伸缩器。
压测是一个 owner token 的并发 VU，并非 200 个独立用户。

## 回归检查与实际限制

- 后端单元：210 passed，83 deselected（integration/model/scale）；不是本次重跑全套容器集成。
- 部署真实 Settings + 五 charts 渲染：4 passed。
- Terraform mock：16 passed，0 failed；没有使用真实 backend state。
- 与当前主线合并后，压测工具 + 实际 k6 localhost 契约：11 passed。
- 前端此前 28 tests 和生产构建通过；两端真实 Cognito 浏览器登录通过。
- Ruff check/format、Terraform fmt、git diff check 已检查；后续改动需在提交前复核。
- Trivy 0.76.0 扫描当前 dev 模块且显式启用 C3：无未豁免 HIGH/CRITICAL。
  保留 C1 的明确 dev 例外；新增仅针对这一个 dev CloudFront 的无 WAF 例外，投产前需完善。
- 监控为私有 ClusterIP，仅端口转发；Prometheus/Grafana 使用 bounded emptyDir。
  导出证据后再缩节点；短期保护原有实例并在缩容后恢复原保护标志，不构成持久化或容灾。
- 部署是操作员驱动。新增 `c3.yml` 检查 chart，不等于合并后自动云端 CD。
- 未调整 MATCH_THRESHOLD，未重新标定 duplicate-photo 数据，也未声称生产容量或匹配质量已验收。

Issue #6 暂不自动关闭：实体相机、邮箱收件及完整浏览器 UC1–UC5 仍有人工核对项。
