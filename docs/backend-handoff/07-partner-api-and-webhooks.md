# Partner API、交付授权与 Webhook 设计

任务：T5。涉及 Claim 和 Notification；Delivery 仍由 Yang Dongwei 负责。先完成 T3/T4，再扩展。该方案不创建额外的独立 Partner 微服务。

## 1. 授权边界

合作方的 OAuth scope 只表示其可以调用某类接口，**不能代替针对某个交付的用户授权**。合作方不得凭 `claim.approved` 事件遍历所有获批认领或读取全部联系方式。

失主在认领获批后主动选择某个合作方及配送方式，创建交付授权。合作方只能访问绑定自己 `client_id` 的有效授权。联系人与收货地址仅在授权后按最小字段返回，普通 Webhook 不携带这些私人信息。

一个认领在同一时刻只有一种交接方式：counter 或 courier。存在活动配送授权时不能再签发现场交接 QR；创建配送授权时原子作废尚未使用的现场 QR。已经 handover_pending/handed_over 的认领不能新建或切换授权。

## 2. 数据模型与归属

| 服务/表 | 字段与约束 |
| --- | --- |
| Claim：`partners` | `id,client_id` 唯一、`name,status,allowed_scopes,version`；运营管理的最小登记目录 |
| Claim：`delivery_authorizations` | `id,claim_id,tenant_id,owner_sub,partner_id,state,encrypted_address,consent_at,version`；每个 claim 最多一个活动授权 |
| Claim：`partner_handover_tokens` | 复用 QR 校验机制，绑定 delivery authorization，独立类型，普通失主 QR 不能冒充 |
| Notification：`webhook_subscriptions` | `id,partner_id,client_id,event_types,endpoint_url,secret_ciphertext,key_id,status,version` |
| Notification：`webhook_deliveries` | `id,subscription_id,external_event_id,authorization_id,payload_json,state,attempts,available_at,lease_token,lease_until`；唯一 `(subscription_id,external_event_id)` |
| Notification：`webhook_attempts` | 投递 ID、尝试号、时间、HTTP 状态、错误码；不记录完整响应体或签名密钥 |

配送订单、物流状态和模拟计费属于 Delivery；Claim 只保存授权与交接依据。地址/联系人加密及访问审计遵循 01。合作方全局目录由独立权限维护，不赋予跨服务数据库读取能力。

## 3. 外部 API

所有动作要求幂等 key；修改现有资源同时校验版本。

| 方法与路径 | 身份 | 核心契约 |
| --- | --- | --- |
| `GET /v1/delivery-partners` | owner | 仅返回活跃合作方 ID、显示名称 |
| `POST /v1/claims/{id}/delivery-authorizations` | 认领 owner | `partner_id,address`；201，授权 ID、状态 |
| `POST /v1/claims/{id}/delivery-authorizations/{authorization_id}/cancel` | owner | 未取件前取消，协调 Delivery 后才完成 |
| `GET /v1/partner/deliveries` | 合作方 + `deliveries.read` | 仅属于本客户端的活动交付摘要 |
| `GET /v1/partner/deliveries/{authorization_id}` | 同上 | 该授权许可的物品、取件场馆及最小地址信息 |
| `POST /v1/partner/deliveries/{authorization_id}/pickup-token` | 已分配合作方 | 生成绑定该授权的取件码，返回给模拟快递员 |
| `POST /v1/partner/deliveries/{authorization_id}/status` | 合作方 + `deliveries.update` | `in_transit/delivered/failed` 等 Delivery 定义状态；不得绕过真实取件确认 |
| `POST /v1/partner/webhook-subscriptions` | 已登记合作方 | `endpoint_url,event_types[]`；创建待验证订阅 |
| `PATCH /v1/partner/webhook-subscriptions/{id}` | 订阅所属合作方 | 启停、地址、订阅事件变更 |
| `POST /v1/partner/webhook-subscriptions/{id}/rotate-secret` | 订阅所属合作方 | 轮换密钥，旧密钥短期共存 |

合作方接口位于对应服务的 router 中；网关以路径明确路由。`deliveries` 的读取/取件授权由 Claim 提供，运输状态路由到 Delivery；OpenAPI 汇总中必须标明实际服务，不能两边实现相同的写入职责。

注册、冻结合作方由 `platform_admin` 运营 API 完成，不允许合作方自行声称某个 `client_id` 属于自己。scope 简写均加 `lostlink/`。

## 4. 内部授权与取件

Claim 提供：

`GET /internal/v1/delivery-authorizations/{id}/partner-context`

只允许 Notification/Delivery 的 M2M 客户端和 `lostlink/partners.authorize`。响应为 `authorization_id,claim_id,item_id,tenant_id,partner_id,partner_client_id,state`，默认不返回地址。需要配送地址的 Delivery 使用单独受审计的最小地址接口；Notification 无权读取。

现场员工使用同一受保护 handover 入口提交取件码。Claim 检查活动授权和合作方绑定，按 05 的协调流程调用 Item commit-handover，`mode=courier`。只有场馆确认成功才算已取件，合作方自行发送 `picked_up` 不能改变 Item 或 Claim 状态。

`claim.handed_over` 触发 Delivery 的 picked_up 状态；之后运输中的状态由 Delivery 维护。Claim 的 handed_over 表示场馆已移交物品，不等同于配送已送到失主。

取消配送授权必须与 Delivery 确认未取件，使用持久协调操作。取消与取件竞争时由 Claim 的行锁/状态条件确定一个赢家；已受理交接不能取消。取消生效后禁止新的合作方读写及普通业务 Webhook，仅允许第 5 节规定的最小撤销通知，但无法撤回合作方此前合法取得的数据。

冻结合作方后立即拒绝新查询、取件码及状态更新；已有任务标记待运营处理，不能自动改派给其他合作方。

## 5. 外部事件契约

对外信封独立于内部领域事件：

```json
{
  "event_id": "73009e0a-14b7-47ca-856e-d41299100467",
  "event_type": "delivery.ready_for_pickup",
  "schema_version": 1,
  "occurred_at": "2026-10-20T09:00:00Z",
  "data": {
    "authorization_id": "a9ca7109-0295-4bf7-aec3-b7d497d11666",
    "item_id": "2c7072b3-8f6b-4b93-b9fc-1b2ba521a46c"
  }
}
```

首期事件为 `delivery.ready_for_pickup`、`delivery.picked_up`、`delivery.cancelled`。不附 owner ID、邮箱、电话、地址、隐藏答案或 QR。合作方要获取许可详情时另调受保护 API。

单独 `claim.approved` 不会给所有合作方广播；只有活动交付授权才能产生该合作方的 pickup 通知。T5 需新增内部 `claim.delivery_authorized` 和 `claim.delivery_revoked`，来源 `claim-service`，载荷为 `authorization_id,claim_id,item_id,partner_id`，要求 tenant。注册、消费者支持及部署顺序遵循 02，不能直接向现有严格载荷追加字段。

投递前再检查授权仍有效且与订阅合作方一致。取件完成后的事件只投递给原有授权合作方；授权撤销通知只含最小撤销信息，不额外开放数据查询。

## 6. 签名与重试

每个订阅独立随机签名密钥，经加密保存。签名基于实际发送的 UTF-8 原始字节，禁止接收方解析再重新序列化后验签：

`signed_payload = timestamp + "." + event_id + "." + raw_body`

`signature = HMAC-SHA256(secret, signed_payload)`，十六进制编码。

请求头固定为 `X-LostLink-Event-ID`、`X-LostLink-Timestamp`、`X-LostLink-Key-ID`、`X-LostLink-Signature`（`v1=<signature>`）。每次尝试更新时间戳与签名，逻辑 event ID 和业务载荷不变。合作方校验 5 分钟时钟窗口并按 event ID 去重。轮换时新旧密钥最多共存 24 小时，并提供旧 key ID 的识别方式。

HTTP 2xx 视为对方已接收；408、429、5xx 和网络错误有限重试，其他 4xx 停止自动重试并记录失败，410 停用订阅。连接超时 2 秒、读取 5 秒，响应读取上限 64 KiB；最多 8 次尝试，退避从 10 秒开始、上限 1 小时，最长生命周期 24 小时。

超时可能导致重复投递，因此不承诺一次。超过重试预算的任务保留为 failed，运营可带审计重放同 event ID。普通伙伴不能要求向任意 URL 重放。

## 7. 目标地址验证

只允许 HTTPS；首期仅允许由运营批准的域名及 443 端口。禁用 HTTP 重定向。拒绝含用户名密码、fragment 的 URL，拒绝回环、私网、链路本地、云元数据、保留及其他非公网 IP。

创建/修改订阅及每次连接前都验证 DNS 解析；连接必须绑定已验证的目标地址并校验原域名 TLS/SNI，避免“检查公网 IP 后客户端重新解析到内网”的间隙。若所选 HTTP 库无法保证这一点，使用受控出站代理或网络策略，不以一次字符串校验代替。

本地模拟合作方通过独立的 test/local 传输适配启用固定内部目标，不能让生产配置复用“允许私网”开关。对已批准公网目标的订阅验证也属于外部请求，代码任务默认用本地替身验证。

## 8. T5 验收与移交

- Partner A 不能枚举或读取 Partner B 的交付；scope 足够但缺少资源授权仍被拒绝。
- 批准认领但未授权配送，不会触发合作方通知或泄露联系方式。
- counter QR 与 courier 授权互斥；重复取件码、取消竞态、伪造 picked_up 不会重复交接。
- 原始消息验签通过；正文、时间戳、事件 ID 被改动时失败；重放按 event ID 去重。
- 测试重定向、私网目标、DNS 变化、超大响应和密钥轮换。
- 明确提交 Claim/Notification/Delivery 三者的接口映射及模拟合作方验收步骤；不实现真实支付或真实快递平台接入。
