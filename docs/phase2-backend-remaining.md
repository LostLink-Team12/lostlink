# Phase 2：JIANG YIBAI 剩余工作与集成交接

核对日期：2026-10-22。以 [Issue #12 分工](https://github.com/LostLink-Team12/lostlink/issues/12)
和 [Issue #7 验收](https://github.com/LostLink-Team12/lostlink/issues/7) 为准。
截至此次检查，`main` 仍为 `137cd00`；下列 #13–#17 均为 open，不能把 PR 内实现视为已合并、已部署或已完成联合验收。
队友的 AWS 运行结果在下文明确标为其 PR 记录，本次未重新访问 AWS 验证。
Issue #12 给 B 的时间目标仍是 10/23 联调、10/24 修复，10/25 参与全员彩排。

## 已有成果及剩余边界

| 工作 | 当前证据 | 仍需完成 |
| --- | --- | --- |
| OpenAPI 错误响应、契约 CI、SQS 超时/重试和 DB 时钟 | 你的 [PR #14](https://github.com/LostLink-Team12/lostlink/pull/14) 已实现，尚未合并 | 跟进审阅、CI 和合并；本部署验证 PR 不重复引入其改动 |
| Cognito 部署产物与入口测试 | 当前分支有 Linux Lambda ZIP 构建、摘要、RIE + PostgreSQL 入口测试 | 与 #15 的 Secrets Manager wrapper 区分入口包，按最终源码部署并验收真实登录 |
| 数据库角色、迁移、密钥预检 | 当前分支的 `backend_admin.py`，含重复执行和权限漂移集成测试 | 用云端现有角色/秘密复检，保留 TLS、RLS、凭据及 keyring 的验收结果 |
| Issue #7 搜索验证 | 当前分支的独立 k6、真实 CLIP/pgvector/HTTP fixture、500 ms 门槛 | 本地 100 次逐条 Item 调用的 p95 为 1497.52 ms；优化及云端复测尚未完成 |

本分支完成的是可重复验证能力，不应自动关闭 Issue #7。

## 队友的 PR 已覆盖哪些工作

| PR / 对应 Issue | 已提供的实现或记录 | 你需要跟进的接点 |
| --- | --- | --- |
| [#13](https://github.com/LostLink-Team12/lostlink/pull/13)，#5 | Terraform 基础设施；作者记录 EKS、私网 RDS/pgvector、ECR 等已实机验证 | 跟进合并和镜像发布，取得部署输出；不另写一套 Terraform |
| [#15](https://github.com/LostLink-Team12/lostlink/pull/15)，#5/#7 | 依赖 #13；Cognito V2 wrapper、4 个 scoped M2M client、15 个 runtime DB role、Claim keys、18 个进程 IRSA、事件路由和 SES。作者记录真实 M2M、RDS 权限、触发器冷/热调用和 SES 身份验证成功 | 复核应用配置和凭据边界；继续真实人类登录、服务间授权、完整邮件流程。SES 身份已验证的记录不等于业务邮件已送达 |
| [#17](https://github.com/LostLink-Team12/lostlink/pull/17)，#6 | Helm、进程级秘密注入、内部 HTTPS、CPU HPA、PostgreSQL KEDA，以及 Claim outbox/Notification consume 启动校验修复 | 审阅这两项应用修复并参与 dev 启动验收，无需再重复实现。当前分支与其组合的 25 项相关单测已通过；这不等于 chart 或云端已验收 |
| [#16](https://github.com/LostLink-Team12/lostlink/pull/16)，#8/#9/#10 | 拒绝、交接、员工管理、邮件深链接、关闭报告；102 对标定数据；200 VU、5,000 图及证据采集脚本 | 后端兼容和真实联调；不接管前端、重新造标定数据或另写一套 scaling demo。PR 自述尚无真实 dev 压测/扩容证据 |

#15 当前 base 为 `codex/terraform-dev`，先合 #13 后应改回 main 并复查。
#17 仍需部署方完成 ingress/domain、Owner HTTPS origin、监控和 KEDA 只读数据库身份等交付。
#16 的数据尚未转化为最终 CLIP 标定结果或部署阈值，#9 的后半段仍由架构负责人完成。

## 剩余工作，按执行顺序

### 1. 完成 PR 集成与兼容性审阅（现在可做）

- 跟进 #14 与当前部署验证分支的审阅、CI 和合并，避免把“分支已做”写成“main 已有”。
- 核对 #17 的 Claim outbox 仅使用 DB/topic；Notification consume 仅使用 DB/queue。
  tasks/API 仍必须校验各自的 M2M、加密/交接密钥及邮件依赖，不能注入假值或扩大 IAM 来绕过启动校验。
- 核对 #15 的浏览器客户端与机器客户端白名单、秘密 JSON 到环境变量的映射、迁移身份与 runtime 身份分离。
  Lambda 的两个产物入口不是直接替换关系，见[部署交接差异](cognito-trigger-deployment.md)。
- 合并只处理当前 main 与目标 PR 的真实冲突；其他 open PR 可做组合验证，但不要提前把全部队友分支混进本 PR。

### 2. 解决搜索逐条 Item 校验的性能风险（设计、实现和回归可先做）

本地受控场景已满足足量有效采样并超过 500 ms，所以这项还没有完成。
优先评估受限候选列表的 **batch internal API**；若选择短缓存，先明确状态变更失效和一致性边界。
优化需单独实现和审阅，不能只提高门槛或缩小结果来隐藏开销。

- 保留 Item 当前 state、tenant、item_version、processing correlation 的校验；过滤条件和排序语义不变。
- 覆盖跨租户候选、撤回/预留/已交付、版本过期、缺失物品、下游失败、未授权机器客户端等回归；不返回参考答案或其他私密数据。
- 使用本分支同一 k6 矩阵做优化前后对比，确认实际调用数下降、结果正确，不能把控制服务器或人工延迟当云端指标。
- #6 部署就绪后，在相同模型、数据、资源和并发下做 dev text/photo/fused 复测；按 Issue #7 的 500 ms 判定。
  #16 的 5 秒 scaling demo 门槛不替代本项，正式验收也不能只用一次本地结果。

### 3. Cognito 人类登录与 M2M 联调（依赖 #15/#17 的实际部署）

- 由管理员本人完成首次登录改密；取得 Owner、Venue、运营和最小权限测试身份，不共享密码或客户端密钥。
- 核对真实 access token 的 groups/tenant、Owner 无 venue 身份、员工晋升/停用和场馆停用后的刷新行为。
  旧 token 的敏感写入仍应经过实时成员校验；ID token 不能代替 access token。
- 验证全部接收服务的 M2M 正向调用和错误 scope/错误 client 的拒绝，不能只验证 token endpoint 能发令牌。
- 复查最终部署 ZIP/hash、wrapper、RDS TLS、冷/热时限和失败关闭；不重复覆盖队友已绑定的 Cognito 配置。
- 保存不含令牌/密码的结果，再更新 Issue #7 对应验收项。

### 4. 云端数据库与密钥复检（依赖可用的私网操作环境）

- 复用 #15 已建立的 15 个 runtime role 与现有秘密，按最终合入代码检查迁移 head、无 DDL/no-bypass、schema 隔离、FORCE RLS 和私密列权限。
- 在授权的操作进程内映射公开配置、读取秘密并运行 `check`/`keys`；不为“跑通脚本”覆盖密码、移除旧加密 key 或临时生成交接 key。
- #15 的 AWS bootstrap/verify 与本工具的公开 JSON/环境接口不同，先做映射；不要在不明确身份/DDL 权限时重复 bootstrap。
- 确认 API/worker 不获得迁移凭据；Cognito identity 是只读角色；#17 的最小秘密集能真实启动。

### 5. SES 与前后端完整闭环（依赖 #16/#17）

- 使用已验证发件人/沙箱收件人，确认真实失物报告和匹配产生 `match.found`，经过 Notification inbox/tasks 后邮件送达。
- 核对重试/幂等不会重复发信、失败状态可追踪；邮件 `/matches/{id}` 链接能登录后恢复并展示正确物品。
- 与前端负责人跑 UC1–UC5：员工管理 → 登记/上传 → embedding/搜索 → 认领/拒绝或批准 → 扫码交接。
  同时覆盖 If-Match 冲突、权限拒绝和停用场景；修复实际暴露的后端问题，更新契约时重跑导出检查。

### 6. 演示支持与最后验收（10/24–10/25）

- 支持 #10 的真实压测和扩容定位；D 负责执行及导出，C/A 负责 HPA/KEDA/监控。
  Matching tasks 的伸缩信号是 PostgreSQL runnable embed jobs，不是 SQS 深度。
- 支持 #11 对接现有 `claim.approved/cancelled/handed_over` 契约和 courier 交接约束；
  delivery 持久化、事件消费者和 Seed 2 演示仍由 A 负责。
  ADR-0009 将寄送授权交给 B，当前 main 还没有持久化的 owner → 指定 partner 授权入口。
  与 A 明确最小授权/合作方接口切分后补齐你负责的一段；不能把 `claim.approved` 自动当作寄送授权，
  也不能让 partner 自行标记已取件。这是 Seed 2 的实际协作缺口，不等同于实现完整 Partner 平台。
- 向架构负责人提供后端验收、搜索优化前后数据、已知限制和真实工时，参加 10/25 彩排。
- 若触发 Issue #12 的云端兜底，配合本地全链路演示；本地结果不能包装成 AWS 扩缩容实测。

## 当前分支审阅结论与合并顺序提醒

- 当前分支包含三步部署验证成果。审阅补充了本地 fixture 的连接参数预检：在第一次管理员连接前拒绝
  `hostaddr`、`service`、`user`、`options` 等目标/身份覆盖参数，增加无网络回归测试。
- 临时 `git merge-tree` 检查未修改工作区或合并任何 PR：与 #17 无文本冲突；与 #14 冲突于
  `services/venue-service/tests/test_venue_integration.py` 和 `tests/load/README.md`；与 #15 冲突于
  `infra/README.md`；与 #16 冲突于 `tests/load/README.md`。
- 上述是当前分支快照的预计冲突，合并后需按新 main 重新检查。保留两套新增测试、Issue #7 的
  500 ms 调查入口及 D 的 scaling demo 文档，不通过整体覆盖 README 或测试文件解决。
- 建议优先跟进 #14，再更新当前部署验证 PR；#13 → #15 按 stacked 依赖推进；#17/#16 再进行联合部署验收。
  不在当前 PR 中直接合并这些尚未审批的分支。

## 当前不应额外接管的范围

旧 proposal 的完整 Partner API、通用 Webhooks 和 Analytics dashboard 未列为 Issue #12 给 B 的 Phase 2
必交内容。以当前 Issue 为准，不把它们加到你的本轮必须完成清单；如 Seed 2 接入确需新增后端契约，
由 #11 明确最小缺口后再实现，不扩展成完整合作方平台。
