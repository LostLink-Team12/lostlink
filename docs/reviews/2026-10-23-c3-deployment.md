# C3 dev 部署与真实验收记录

记录日期：2026-10-22 至 2026-10-23（Asia/Shanghai）。关联 #6、#10、#15、#16、#17、#24。

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
收尾时同步了主线 #14/#18/#19；云端镜像仍明确对应 `62c0f4d` 的已验收构建。
主线新增的错误契约、消费者恢复和搜索阶段诊断由合并后的本地/CI 回归覆盖，未冒称已部署压测。

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
| 浏览器 | 员工角色修改往返、物品照片登记、照片/文字搜索、保存报告、通知深链接 Cognito 回跳、回答问题、审批、二维码生成与原码交接通过；Claim/Item 均 handed_over |
| 公网边界 | 15 项 HTTPS、路由与私有接口拒绝检查通过 |

两节点恢复后重新执行 API 业务流与纯文字匹配流，通过后均关闭报告。浏览器另创建
`C3 browser suitcase`：真实登记、S3 上传确认并变为 available；搜索找到该物品；报告匹配
分数 0.9068，Notification inbox 可见；`/matches/:id` 登录后保持原路径并展示对应物品。
随后通过页面提交答案、关闭报告、场馆审批、失主生成 QR、场馆输入同一原始 payload 交接。
Claim 与 Item 均为 handed_over，截图 `browser-handover.png` 不含交接码。码值只在进程内存中
传递，没有导出。四条 fixture owner 报告全部关闭后，notification-tasks 已恢复 1/1 Ready。

没有向其他人发送消息或邮件。真实邮箱收件、浏览器场馆入驻表单及实体摄像头扫码仍需人工验收；
场馆入驻已通过 API 验证，本次交接使用原始 QR payload，不声称实体摄像头已通过。

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

当前 4 GiB 预留配置另实跑 256 张新导入（`c3-current-worker-20261023-v2`），
全部确认、HTTP 无失败，k6/collector 均退出 0。29 次采集无错误，runnable 峰值 13，
Matching tasks desired/Ready 实际从 1 到 2 再回 1；Item API 从 1 到 5 再回 1。
历史 failed 仍为 7，积压排空且冷却稳定。该轮库存为 5,256 available、1 handed_over，
是当前配置的补充扩缩容证据，不是另一次 5,000 张导入。此前同 RUN_ID 的首次尝试因
6 iterations 小于默认 10 VU 被 k6 拒绝，未导入数据；保留失败记录，并修复小批次 VU 上限。

## 搜索与扩容

首次两节点、最多两个 API 副本的默认 13 分钟 0 → 25 → 50 → 100 → 200 → 0 VU 运行：
9,440 请求，12.073 req/s，p95 19,255.996 ms，16 次超时。严格响应契约和 5 秒 p95 目标失败。
44 次采集无缺失，API 1 → 2 → 1，冷却已观察。该轮当时没有完整 5,000 件库存。

修复了同步推理挤占线程池时阻塞探针的问题：health/metrics 异步，ready 使用独立执行器，
已加载模型的 ready 不再等待推理锁。实际运行还修复了 SQS 长轮询的过短 read timeout，
并按进程拆开 Claim outbox/Notification consume 启动校验，没有注入假凭据。

以下复测均在完整 5,000 件可搜索库存上进行，使用原始 13 分钟阶段和严格阈值
（HTTP 错误率 <1%、整体 p95 <5,000 ms、有效搜索 100%）。失败记录均保留，不能仅凭扩容判为通过。

| 运行 | 容量与配置 | 请求数 / req/s | p95 ms | HTTP 失败率 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 初次 | 2 节点，Matching 上限 2，库存未满 | 9,440 / 12.073 | 19,255.996 | 0.1695% | 失败 |
| v2 | 5 节点，Matching 8、Item 2；历史 2500 MiB 模型预留 | 8,620 / 11.029 | 13,407.688 | 34.4200% | 失败 |
| v3 | 5 节点，Matching 8、Item 8；历史 2500 MiB 模型预留 | 11,113 / 14.221 | 13,306.957 | 10.6452% | 失败 |
| v4 | 9 节点，Matching 8、Item 8；4 GiB 模型预留 | 14,535 / 18.592 | 9,470.191 | 1.8782% | 失败 |
| v5 | 同 v4；去除向量传输、复用 TLS 上下文 | 15,115 / 19.328 | 11,751.581 | 3.5131% | 失败 |
| v6 | 同 v5；启用无过滤 HNSW 检索 | 15,440 / 19.739 | 11,939.383 | 1.1010% | 失败 |
| v7 | 同 v6；Item 内部池 2，用户池 1 | 15,531 / 19.858 | 11,849.308 | 0.9401% | 失败 |

v2/v3 是早期文字压测配置，不是大图容量保证。合入 #17 后，模板强制模型 requests/limits
至少 4 GiB，默认 Matching API/tasks 各上限 2。t3.large 的可分配内存只能容纳一个这样的
模型 pod，故八 API 加一个常驻 worker 的压测需要临时九节点；HPA 上限本身不能创造容量。

v4 期间 RDS CPU 峰值 90.28%，网络输出峰值约 19.78 MB/s。查询原来额外返回 100 个
512 维候选向量，调用者只使用它们的版本元数据和数据库已算出的分数。优化后保留数据库
评分、过滤和排序，禁止延迟加载这些向量。另复用经过证书验证的 SSLContext，消除每个
Item 查询重复加载 CA 的 CPU 成本；每次仍读取当前状态、使用当前授权，没有响应缓存。
实际镜像、真实 CA bundle 的旧上下文创建成本约 8.89 ms CPU/次。新增真实 HTTPS 测试
验证状态与 bearer header 更新，且错误主机名和不受信任 CA 仍被拒绝。

v5 使用提交 `fed9245325d2bfcd05e5e4da131f30c5c8ac90f1` 的干净构建。数据库网络输出峰值
降至约 2.22 MB/s，但峰值阶段仍失败，Item 记录了连接池 TimeoutError。进一步的真实执行
计划确认：第二排序键让原查询没有使用 HNSW。空载重复测量中，全扫描约 45–52 ms，
单距离索引查询约 6–11 ms。新的有界内层查询只按原始向量距离排序，保留 RLS、状态、
版本、租户和模型过滤，再对最多 100 个候选做稳定排序与元数据读取；实际计划约 14.9 ms。
有分类、场馆或日期过滤时保留原精确检索，避免 pgvector 0.7 对高选择性过滤的召回限制。
同分候选在截断边界的成员可能变化；候选集合内稳定按距离与 ID 排序，原有截断提示保留。

v6 的搜索中位数降至 974 ms，但 Item 连接池排队仍使尾延迟和严格响应契约失败；170 个请求
失败，不能把中位数改善当作验收成功。原始汇总和冷却证据继续保留。

峰值 Item 内部请求还暴露了单连接池的排队超时。RDS 实测总连接上限 79、保留 3，不能
简单把全局池调大。新增独立 `INTERNAL_DB_POOL_SIZE`，仅将 Item 内部池从 1 调到 2；
用户 API 与其他进程维持 1，overflow 均为 0。按八 Matching API 的压测上限加上其他
HPA/KEDA 上限，应用池合计 64，另留监控 2、实测后台 8、操作员 2 和保留连接 3。
部署契约检查该预算；不要与迁移并行运行该压测配置。v7 已验证此配置，尾延迟未明显改善，
因此不能将单连接池视为唯一性能瓶颈。

v7 的 HTTP 失败率已低于 1%，但 p95 和 100% 有效搜索仍未达标（146 个失败请求）。
API 扩容与自动回落已实际发生，不能将它表述为 200 VU 性能验收通过。5 秒尾延迟及严格契约
仍是未完成的性能项，原始结果和截图全部保留；本次不自动关闭 #10。

KEDA/HPA 不提供节点容量；本次节点容量由操作员通过 Terraform 调整，没有安装节点自动伸缩器。
压测是一个 owner token 的并发 VU，并非 200 个独立用户。

取证后已应用 Terraform 基线恢复计划（0 新增、1 更新、0 删除），节点 desired 9 → 2、
max 9 → 5。最终 EKS ACTIVE、两节点 Ready、22 个 Deployment 全部就绪，原实例均 InService，
临时 scale-in protection 已恢复为原 false。Matching API HPA 上限恢复 2；没有遗留九节点压测容量。
两节点能容纳最低两个 4 GiB 模型进程，无法同时承载四个模型副本；扩容前仍需操作员增加节点。

## 回归检查与实际限制

- 同步最新主线后的后端单元：314 passed，111 deselected（integration/model/scale）；本地未重跑全套容器集成。
- 部署真实 Settings + 五 charts 渲染及峰值连接预算：5 passed；发送器暂停安全检查另 5 passed。
  验收工具同时要求 Deployment 为零且没有运行中或终止中的 sender Pod，避免仅改副本数就开始报告测试。
- Matching 与 Item 的真实隔离 pgvector 集成：23 passed、6 skipped（本地未启用 LocalStack 事件/媒体端点）；
  覆盖跨场馆、过滤、版本墓碑、持久化匹配、通知复核及 Item 内部池 2。临时测试容器已删除。
- Terraform mock：16 passed，0 failed；没有使用真实 backend state。
- 与当前主线合并后，压测工具 + 实际 k6 localhost 契约及 search-review 脚本：17 passed。
- 前端此前 28 tests 和生产构建通过；两端真实 Cognito 浏览器登录通过。
- Ruff check/format、Terraform fmt、git diff check 已检查；后续改动需在提交前复核。
- Trivy 0.76.0 扫描当前 dev 模块且显式启用 C3：无未豁免 HIGH/CRITICAL。
  保留 C1 的明确 dev 例外；新增仅针对这一个 dev CloudFront 的无 WAF 例外，投产前需完善。
- 监控为私有 ClusterIP，仅端口转发；Prometheus/Grafana 使用 bounded emptyDir。
  导出证据后再缩节点；短期保护原有实例并在缩容后恢复原保护标志，不构成持久化或容灾。
- 部署是操作员驱动。新增 `c3.yml` 检查 chart，不等于合并后自动云端 CD。
- 承接 #17 的标定配置：照片/融合阈值 0.78，纯文字 0.63；新增两条持久化匹配路径对文字阈值
  的消费及回退测试。未重新标定 duplicate-photo 数据，也未声称生产匹配质量已验收。
- 同一真实镜像以只读根目录、无网络、1 CPU、4 GiB 无 swap 限制测试 16 MP 图片的文字/
  照片/融合推理：API 峰值 RSS 2741.34 MiB，tasks 2732.86 MiB，均无 OOM。
  cgroup 峰值读取不可用，记录为 null，未将 RSS 当作容器总内存；证据固定引用当时镜像 manifest。

Issue #6 暂不自动关闭：实体相机、邮箱收件和浏览器场馆入驻表单仍需人工核对。
