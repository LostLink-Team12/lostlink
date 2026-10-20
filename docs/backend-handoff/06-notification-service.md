# Notification Service 邮件设计与实施任务

任务：T4。目录：`services/notification-service/`。覆盖 UC4 的通知部分。匹配算法属于 Matching，本服务只消费匹配结果。Webhook 是同服务后续 T5 模块。

## 1. 数据模型

| 表 | 字段与约束 |
| --- | --- |
| `notifications` | `id,tenant_id,owner_sub,match_id,report_id,item_id,channel,template_version,state,created_at,sent_at`；唯一 `(match_id,owner_sub,channel)` |
| `notification_attempts` | `id,notification_id,attempt_number,lease_token,started_at,finished_at,result,provider_message_id,error_code` |
| `notification_jobs` | `notification_id` 唯一、`available_at,attempts,lease_token,lease_until,state` |
| 公共表 | inbox、审计；需要向外发布事件时才增加对应 outbox |

收件邮箱在发送时经可信身份目录解析，默认不复制到普通通知表；如为重试保存地址，必须加密且有到期清理。日志、指标和错误信息都不出现邮箱、电话或邮件全文。

通知状态采用 `pending → sending → sent`，失败可进入 `retry_wait`、`failed`、`delivery_unknown` 或 `suppressed`。状态是本服务观察到的发送结果，不等于用户已经阅读。

## 2. `match.found` 的处理

消费事件先执行 02 的 inbox 逻辑，再按业务唯一键创建通知及发送任务。同一事件重复投递不重复处理；不同 event ID 对同一 match 的重复报告也只生成一个通知。

消费者检查允许发布方为 Matching、必要租户存在、载荷与契约一致。事件数据只用于定位匹配结果，不能把任意 `owner_id` 当作允许向第三方发送内容的完整授权。

发送前通过内部匹配接口校验：

`GET /internal/v1/matches/{match_id}/notification-context`

调用者为 Notification 专用 M2M 客户端，scope 为新增 `lostlink/matches.notify`。响应仅含 `match_id,owner_id,report_id,item_id,tenant_id,active,item_title,venue_name`。这些 ID 必须与事件一致。Matching 还需检查当前 Item 可用性；失物报告已关闭、匹配撤销、物品已交接时返回 `active=false`。

UC3 实施更新：该接口已在 Matching 实现并验证 Item 当前状态、版本及轮次。现有 Venue 尚无机器可读的公开名称目录，因此 `venue_name` 明确允许 null；首版邮件使用通用场馆标签或省略名称，不能展示 UUID 冒充名称。后续增加经过授权的 Venue 目录后再补值，见 [09：Matching/Embedding](09-matching-and-embedding.md)。Notification 仍需独立实现，不能把此接口完成视为邮件已发送。

未就绪接口可用契约替身验证，但不得在真实发送路径跳过检查。校验失败或资源不再相关时标 `suppressed`，依赖超时则重试。

## 3. 邮件内容与地址来源

使用共享 `IdentityDirectory.resolve_verified_email(owner_sub)` 获取当前用户已验证邮箱。没有用户或没有已验证邮箱时标记明确原因，不回退到事件或请求中任意邮箱。

共享接口已实现（见 [01 第 6、8 节](01-contracts-and-access.md)）：`IdentityNotFound`、`EmailNotVerified` 对应终态抑制/失败原因，`IdentityDirectoryUnavailable` 对应可重试；错误 `code` 可直接作为通知的原因码，不记录邮箱。

初版模板 `match-found-v1` 内容包括物品公开标题、场馆名称和平台详情链接。链接由固定 `OWNER_APP_BASE_URL` 和 match ID 拼接，不能使用消息提供的任意域名。邮件不附隐藏核验细节，不承诺已经证明物品归属。

HTML 插值必须转义；同时提供纯文本部分。邮件标题和模板在代码中采用英文。默认不附图片，不发送手机号、QR 或认领答案。

发件人来自经过 SES 验证的配置。LocalStack 当前未配置 SES，因此单元/集成测试使用捕获发送器；AWS 测试只在明确授权、验证发件人和测试收件人之后执行。

## 4. 发送与不确定结果

工作者使用 60 秒租约认领任务，事务外调用发送器，完成后按 lease token 更新结果。发送尝试上限 5 次，退避从 30 秒开始、上限 30 分钟；终态错误（邮箱未验证、模板无效等）不盲目重试。

把失败区分为：

- **明确未发送**：连接建立前失败、服务明确拒绝；按允许规则重试。
- **明确已接受**：收到供应商消息 ID，记录 sent。
- **结果未知**：请求可能已被接受但响应丢失，或进程在调用后落库前崩溃；标记 delivery_unknown，记录尝试，不自动宣称 sent。

首期对 delivery_unknown 不立即自动重发，进入带审计的运营确认/重试流程，以降低重复邮件。运营决定重发时复用通知 ID、新增发送尝试，并明确可能重复。供应商的接收结果与本地事务无法原子提交，不承诺严格一次邮件送达。

对崩溃留下的 sending 任务，不把所有情况都当成“尚未发送”。任务表须区分尚未开始外部调用与已开始尝试，后者租约过期进入 delivery_unknown。

## 5. API 与运维

| 方法与路径 | 身份 | 行为 |
| --- | --- | --- |
| `GET /v1/notifications` | owner | 本人的最小通知列表：ID、匹配引用、状态、时间 |
| `GET /v1/notifications/{id}` | owner | 本人通知状态；不暴露供应商错误原文 |
| `GET /v1/admin/notifications` | 运营 | 按状态查询任务元数据，不默认显示地址 |
| `POST /v1/admin/notifications/{id}/retry` | 运营 | 幂等、带原因码地恢复 failed/unknown；需明确重复风险标记 |

普通员工无权查询失主邮件地址或通知正文。运营接口也不提供任意收件人发信功能。

worker 指标包括待处理数、最老任务年龄、失败/未知结果次数、抑制次数和发送延迟；指标标签不用 owner、邮箱、item ID。工作者探针除进程状态外检查数据库及所需队列配置。邮箱身份/SES 配置不完整时发送器保持未就绪，不能返回虚假的成功消息 ID。

## 6. T4 验收与移交

- 相同 match 通过重复 event ID 和不同 event ID 投递，最多生成一个通知任务。
- 事件与权威匹配数据中的 owner/item/tenant 不一致时不发送。
- 报告关闭、物品失效、邮箱未验证时得到明确抑制或失败原因。
- 邮件插值不能注入 HTML，链接域名不能由事件更改。
- 模拟供应商超时与进程崩溃，delivery_unknown 可见且不会无限自动重发。
- owner 只能看自己的通知；员工不能取得收件地址。
- 提供 SES 适配、捕获发送器、模板快照测试、队列与数据库集成测试，以及需 Matching 提供的内部契约。

本任务不实现 Webhook 订阅管理；完成后将相同任务租约与发送尝试模式交给 T5 复用，避免两个模块共用一套含义不明的状态字段。
