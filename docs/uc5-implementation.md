# UC5 实现、验证与移交记录

实施日期：2026-10-20。分支：phase1-backend。设计来源：[05：Claim Service](backend-handoff/05-claim-service.md)。本轮不自动 commit，改动供手动复查。

## 已完成范围

- 新增独立 `services/claim-service`（包 `claim_app`）、`claim` schema、冻结的 Alembic 初始迁移，以及 API、worker 两类受限运行角色。
- 失主：取得核验问题、提交认领（答案 + 可选姓名/电话，已验证邮箱由身份目录取得）、本人列表/详情/联系方式、取消、签发 QR。
- 场馆员工：本场馆审核列表/详情、批准（202 异步）、拒绝、获批后查看联系方式、扫码交接（202 异步）。
- 操作查询：失主或所属场馆员工按 operation ID 查看阶段、认领状态与结果码。
- 协调器：独立 `tasks` 进程按认领状态调用 Item 的 reserve/release/commit-handover，含租约、重试、截止时间、`needs_attention` 与带审计的人工重放；独立 `outbox` 进程投递 `claim.approved`、`claim.cancelled`、`claim.handed_over`。
- 共享能力接入：`actor_session` 区分失主/场馆模式，`FieldCipher` 加密联系人与 QR 重放值，`IdentityDirectory` 解析已验证邮箱，`MembershipClient` 实时校验员工。
- Dockerfile、`.env.example`、服务 README、根测试路径；CI 按目录自动发现新服务。

运行与接入命令见[服务 README](../services/claim-service/README.md)。

## 状态机落地

```mermaid
stateDiagram-v2
    [*] --> submitted
    submitted --> approval_pending: 员工批准（202）
    submitted --> rejected: 员工拒绝
    submitted --> cancelled: 失主取消（同步）
    approval_pending --> approved: Item 预留成功并本地提交
    approval_pending --> rejected: Item 拒绝预留（item_unavailable）
    approval_pending --> cancellation_pending: 失主取消
    approved --> cancellation_pending: 失主取消
    cancellation_pending --> cancelled: Item 确认释放
    approved --> handover_pending: 员工扫码（202）
    handover_pending --> handed_over: Item 提交交接并本地确认
```

## 本轮确定的实现决定

1. **每个认领最多一个活动协调操作**（部分唯一索引）。协调器不按操作类型，而按认领当前状态决定动作：`approval_pending` 预留、`cancellation_pending` 释放、`handover_pending` 提交交接。审批中取消时复用同一操作并改为 cancel，即使远端预留已成功也会继续释放，不会出现两个互相竞争的操作。
2. **同一物品最多一个认领进入持有阶段**：对 `approval_pending/approved/cancellation_pending/handover_pending/handed_over` 建 `item_id` 部分唯一索引。第二个员工批准同一物品时返回 409 `item_claim_in_progress`，该认领保持 submitted，不像原设计那样直接拒绝；前一认领取消或被拒后可继续审批。Item 预留仍是最终权威。
3. **Item 拒绝预留**（物品内容版本变化、已不可用、预留已关闭）时，认领转为 rejected，`decision_code=item_unavailable`。网络错误、超时、Item 503 和 `operation_in_progress` 一律视为结果未知：先查询预留记录，查不到则退避重试，不当作拒绝。
4. **固定幂等键**：`claim:{id}:reserve`、`claim:{id}:release`、`claim:{id}:handover:{handover_id}`；释放原因固定 `claim_cancelled`，保证重试请求体不变。
5. **重试与截止**：60 秒租约；指数退避上限 5 分钟；10 次尝试或已开始操作超过 15 分钟后标 `needs_attention`，认领保留中间状态。仅因 worker 停机而未尝试的操作不会因截止时间被直接判定失败。`needs_attention` 只能通过 `tasks --retry-id` 带操作人和原因重放。
6. **自审/自交接禁止**：员工令牌的 subject 等于认领 owner 时返回 403 `self_review_forbidden`；RLS 的“失主或场馆”叠加可见由业务层显式过滤（场馆查询固定 `tenant_id`，失主查询固定 `owner_sub`）。
7. **拒绝原因**：`answers_incorrect`、`insufficient_detail`、`duplicate`、`other`。拒绝不发布事件（注册表无 `claim.rejected`）。
8. **QR**：内容 `LL1.{claim_id}.{token_id}.{32 字节 base64url}`，数据库只存 `HMAC-SHA256(HANDOVER_TOKEN_KEY)`；默认 24 小时有效。原码以 AES-GCM 加密保存最多 10 分钟供同 key 重放，超时 409 `token_replay_expired`。重新签发原子作废旧码；扫码进入 processing 时清除重放值。并发扫码只有一个 202，另一个得到 409（码已占用）或 412（认领版本已前进），两者都不会产生第二次交接。
9. **联系方式**：提交时把姓名、电话和已验证邮箱加密为一行快照（AAD 绑定 `claim-service/claim_contacts/encrypted_contact/claim_id`）。员工仅在 approved/handover_pending 时可读，由 RLS 与路由双重限制；失主可读本人；每次读取写审计。worker 角色无联系人权限。
10. **事件**：`claim.approved` 仅在本地批准提交时写一次；`claim.cancelled` 用于同步取消和释放后的取消；`claim.handed_over` 固定 `mode=counter`。载荷不含联系方式或 QR。

## 数据库权限

| 角色 | 主要权限 |
| --- | --- |
| API | 认领/答案/操作/QR/交接记录按“本人或当前场馆” RLS；联系人另加获批条件；只可更新状态、版本、审核字段及 QR 状态列；outbox 与审计只能插入 |
| worker | 认领、QR、交接记录按任务租户 RLS，只更新状态类列；操作与 outbox 可认领；无联系人、答案权限 |
| 迁移 | 独立 DDL 凭据，不进入运行容器 |

两角色都不能修改 `tenant_id`、`owner_sub`、`item_id`，不能读取 outbox 正文以外的服务 schema。测试逐项验证了这些 `permission denied`。

## 验证

仓库根目录（Docker PostgreSQL/pgvector 与 LocalStack）：

```powershell
$env:LOSTLINK_TEST_ADMIN_URL='postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink'
.\.venv\Scripts\python.exe -m pytest -q services/claim-service/tests
docker build -f services/claim-service/Dockerfile -t lostlink/claim-service:uc5 .
```

Claim 测试 17 项通过（13 项集成、4 项单元）。集成测试在一个随机数据库中执行 Venue、Item、Claim 三套迁移和九个受限角色，进程内运行真实 Venue 与 Item 应用；Claim 的 Item 客户端与成员客户端通过注入的传输层调用它们。覆盖：

| 场景 | 结果 |
| --- | --- |
| 主流程 | 问题不含参考答案 → 提交 → 审批 202 → 协调器 → approved 且 Item 为 reserved/held → 员工读取联系人 → QR → 扫码 202 → handed_over 且 Item 为 handed_over/committed → 重复扫码 409、联系人关闭 |
| 幂等/版本 | 同 key 同请求重放（物品已预留后仍返回原结果）、同 key 不同请求 409、缺 key/If-Match 428、旧版本 412 |
| 并发 | 两个失主同一物品并发审批只一个 202；并发扫码只一个 202 |
| 恢复 | 远端预留成功后本地未提交（租约过期）→ 重启后批准，Item 只有一条 held；Item 已提交但响应丢失 → 查询预留后批准；Item 不可用 → 重试 → needs_attention → 审计重放后批准 |
| 取消竞态 | 审批中取消 → 释放墓碑 → cancelled；迟到 reserve 返回 `reservation_closed`；物品可被他人重新认领；获批后取消作废 QR |
| 物品变化 | 认领后物品内容版本变化 → 审批得到 rejected/item_unavailable，Item 无预留 |
| QR 攻击 | 被替换旧码、篡改、错误场馆、格式错误、过期、重放窗口过期 |
| 隐私/RLS | 他人/他场馆不可见；M2M、外部客户端、失主访问员工接口均被拒；场馆员工兼 owner 的跨场馆认领不出现在本场馆列表；拒绝后联系人关闭 |
| 其他 | outbox 失败重试保持 event ID；迁移 downgrade/upgrade；未验证邮箱 409、身份目录故障 503、物品已预留 409、题目版本变化 409 |

Linux 镜像 `lostlink/claim-service:uc5` 构建通过；容器 UID 1000，`pip check` 无问题，`/health`=200、缺配置 `/ready`=503、未登录 `/v1/claims`=401，验证后容器已删除。

全仓回归（同时设置 `LOSTLINK_TEST_AWS_ENDPOINT`、`LOSTLINK_TEST_SCALE=1` 与 UC3 的 CLIP 变量，命令同 [01 第 8 节](backend-handoff/01-contracts-and-access.md)）：**251 项通过**，无 skip，6 条警告仍为 UC3 已知的 TorchScript 提示；Ruff 检查、145 个文件格式检查与 `git diff --check` 通过。之后对操作查询接口做了“先按本人、再按场馆”的可见性调整，Claim 17 项与 Ruff 已重新运行通过。

## 已知限制与后续

- **Item 预留观察误报**：Item 在预留 5 分钟后仍为 held 即记录 `needs_attention` 并告警。真实 Claim 下，已批准但尚未现场交接的认领会长期持有预留，因此每个超过 5 分钟未交接的批准都会产生误报。告警不会释放预留，不影响正确性；需决定是否让 Item 对“已由 Claim 确认批准”的预留停止观察（例如 Claim 批准后调用确认接口），或由运维按 Claim 操作状态过滤。已记入团队清单。
- 交接后 30 天清理联系人快照、`pending` 超过 5 分钟的指标告警尚未实现；当前只在预算耗尽时记录 `needs_attention` 日志。
- 快递交接（courier）、交付授权与 Partner 接口属于 T5；当前交接固定 `mode=counter`。
- 真实 Cognito（用户池 `ListUsers`、M2M scope）、密钥存储、SNS 订阅和网关路由未联调；身份目录在测试中使用静态替身。
- Notification（UC4 邮件）尚未实现，`claim.*` 事件当前无消费者。

本轮没有创建云资源、发送消息、commit、push 或 PR。
