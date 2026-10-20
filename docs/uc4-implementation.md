# UC4 实现、验证与移交记录

实施日期：2026-10-20。分支：phase1-backend。设计来源：[06：Notification Service](backend-handoff/06-notification-service.md)。本轮不自动 commit。

UC4 由两部分组成：Matching 在新物品或新报告匹配时产生 `match.found`（UC3 已实现，见 [09](backend-handoff/09-matching-and-embedding.md)），本服务负责邮件通知。

## 已完成范围

- 新增 `services/notification-service`（包 `notification_app`）、`notification` schema、冻结迁移，以及 API、admin、worker 三类受限运行角色。
- `consume` 进程：SQS 长轮询，只接受 Matching 发布的 `match.found`；同一事务写 inbox 回执、通知和发送任务后才删除消息。
- `tasks` 进程：60 秒租约，向 Matching 通知上下文接口复核，经身份目录解析已验证邮箱，渲染 `match-found-v1`，先提交“已开始尝试”再调用 SES。
- 失主接口：本人通知列表和详情。运营接口：按状态列出元数据、统计（各状态数量、最老待发送时长）、带审计和幂等的重试。
- SES 适配（SDK 内部重试关闭）、本地/测试捕获发送器、Dockerfile、`.env.example`、服务 README、根测试路径。

## 本轮确定的实现决定

1. **去重两层**：`(consumer, event_id)` inbox 处理传输重投；`(match_id, owner_sub, channel)` 唯一键处理“不同事件 ID 指向同一匹配”。同一事件 ID 内容不同视为协议错误，不确认消息。
2. **发送前复核**：Matching 返回 404 → `suppressed/match_not_found`；`active=false`（报告关闭、物品不再可用、版本或轮次变化）→ `suppressed/match_inactive`；owner、report、item、tenant 任一与事件不一致 → `suppressed/context_mismatch`。Matching 不可用则退避重试，从不跳过复核。
3. **收件地址**：只用 `IdentityDirectory.resolve_verified_email(owner_sub)`；账号不存在或邮箱未验证 → `failed` 并记录原因码；身份服务故障 → 重试。地址和邮件正文不入库、不进日志。
4. **结果三分**：SES 返回 MessageId → `sent`；明确未发送（MessageRejected、发件身份未验证、配置集错误、发送暂停、连接未建立）→ `failed`，其中限流与连接失败重试；读取超时、SES 5xx、缺少 MessageId、已开始尝试后进程崩溃 → `delivery_unknown`，不自动重发。
5. **SDK 重试关闭**：SES 客户端 `total_max_attempts=1`，避免 botocore 在响应丢失后静默重发造成重复邮件。
6. **崩溃识别**：发送前提交一条无 `finished_at` 的尝试记录；租约过期后若发现未结束尝试，标 `delivery_unknown/worker_interrupted`，任务停止。
7. **重试预算**：5 次尝试，退避 30 秒起、上限 30 分钟（±10% 抖动）；耗尽 → `failed/retry_exhausted`。
8. **运营重试**：仅 `failed` 与 `delivery_unknown`；后者必须 `acknowledge_duplicate_risk=true`；原因码 `dependency_restored`、`provider_confirmed_not_sent`、`owner_requested`；复用通知 ID、尝试次数清零、写审计；没有向任意收件人发信的接口。运营身份需 `platform_admin` 组且客户端在 `OPERATOR_CLIENT_IDS`，并使用独立 admin 数据库角色（参照 Venue platform 角色）。
9. **模板**：英文固定标题，HTML 全部转义并附纯文本；链接只由 `OWNER_APP_BASE_URL`（HTTPS）与 match UUID 拼接；明确“匹配不等于归属证明，需提交认领”。`venue_name` 目前恒为 null，使用通用标签“a LostLink venue”。
10. **部署约束**：`EMAIL_SENDER=capture` 在 dev/prod 被配置校验拒绝；发送器未配置时 worker 拒绝启动，不会返回虚假 MessageId。

## 数据库权限

| 角色 | 权限 |
| --- | --- |
| API | `notifications` 只读，RLS 限定 `owner_sub = app.user_sub` |
| admin | 通知、任务、尝试只读；只能更新通知状态/原因与任务调度列；审计与幂等记录 |
| worker | 通知/任务/尝试的插入和状态类列更新；inbox；不能删除，不能改 owner、match、tenant |

测试逐项验证了 RLS 与 `permission denied` 边界，包括 worker 不能读 Matching schema。

## 验证

```powershell
$env:LOSTLINK_TEST_ADMIN_URL='postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink'
$env:LOSTLINK_TEST_AWS_ENDPOINT='http://localhost:4566'
.\.venv\Scripts\python.exe -m pytest -q services/notification-service/tests
docker build -f services/notification-service/Dockerfile -t lostlink/notification-service:uc4 .
```

Notification 测试 15 项通过（9 项集成、6 项单元），无 skip。集成测试在一个随机数据库执行 Item、Matching、Notification 迁移和九个受限角色，进程内运行真实 Item 与 Matching 应用（确定性测试编码器），`match.found` 来自 Matching 自身的 outbox；Notification 通过真实 Matching 内部接口复核。覆盖：

| 场景 | 结果 |
| --- | --- |
| 正常 | 报告 + 物品 → Matching 产生 `match.found` → 一封邮件，收件人为已验证地址，链接固定域名，标题中的 `<script>` 被转义；同事件重投与不同事件 ID 同匹配都不重复；同 ID 不同载荷拒绝 |
| 复核 | 报告关闭 → match_inactive；未知 match → match_not_found；owner 被篡改 → context_mismatch；均无邮件 |
| 身份/依赖 | 未验证邮箱 → failed；身份目录故障、Matching 故障 → 重试；预算耗尽 → retry_exhausted |
| 发送结果 | 拒绝 → failed；限流 → retry_wait 后成功（两条尝试记录）；超时 → delivery_unknown 且不再自动发送；尝试开始后进程崩溃 → worker_interrupted |
| 运营 | 失主和非运营客户端 403；未确认重复风险 409；确认后重试并发送、幂等重放、非可重试状态 409；列表无地址；统计；审计记录 |
| 消息 | LocalStack SNS→SQS：有效事件入库后删除，无效消息不删除 |
| 其他 | 迁移 downgrade/upgrade；单元测试覆盖模板转义、SES 错误映射（Stubber）、SDK 单次尝试、配置校验与 HTTP 守卫 |

Linux 镜像 `lostlink/notification-service:uc4` 构建通过；UID 1000，`pip check` 无问题，`/health`=200、缺配置 `/ready`=503、未登录 401，worker 模块可导入；验证后容器已删除。

全仓回归（含 LocalStack、5,000 条规模测试与真实 CLIP，环境变量同 [01 第 8 节](backend-handoff/01-contracts-and-access.md)）：**266 项通过**，无 skip，6 条警告为 UC3 已知 TorchScript 提示；Ruff 检查、164 个文件格式检查与 `git diff --check` 通过。

## 已知限制与后续

- 未执行真实 SES：需要已验证发件身份、沙箱测试收件人或生产访问，以及 IAM `ses:SendEmail`。本轮没有发出任何真实邮件。
- worker 指标（待处理数、最老任务年龄、unknown 次数）目前只通过运营统计接口提供，尚未暴露 Prometheus 指标。
- SES 退信/投诉（SNS 反馈）与抑制列表尚未处理；`sent` 只表示 SES 已接受。
- 场馆名称依赖未来的 Venue 公开目录；Claim 状态变化邮件不在本轮范围。
- T5 Webhook 订阅与投递尚未实现，将复用本服务的租约与尝试模式但使用独立表。

本轮没有创建云资源、发送真实邮件或消息、commit、push 或 PR。
