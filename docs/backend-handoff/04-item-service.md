# Item Service 设计与实施任务

任务：T2。目录：`services/item-service/`。覆盖 UC2，并为 UC3、UC5、UC6 提供受保护的接口。前置：T0；Venue 检查可在早期使用明确的契约替身。

实施状态：UC2 服务及本地验证已完成，见 [执行记录](../uc2-implementation.md)和[运行说明](../../services/item-service/README.md)。确认上传采用持久操作并可轮询，批量接口增加稳定 batch_reference 及失败行修复入口；这些补充语义以执行记录为准。CLIP/Matching 由其他服务接入。

## 1. 数据所有权

Item 是物品当前状态、照片版本、核验资料及预留状态的唯一权威。Embedding 拥有向量计算结果，Matching 拥有公开检索读模型。其他服务不能直接更新 Item 表。

| 表 | 核心字段与约束 |
| --- | --- |
| `items` | `id,tenant_id,title,description,category,found_at,public_location_hint,state,item_version,version,processing_correlation_id,photo_id,reserved_claim_id,created_by,created_at,updated_at` |
| `verification_questions` | `id,tenant_id,item_id,prompt,reference_answer,position,version`；只供本场馆员工使用参考答案 |
| `item_photos` | `id,tenant_id,item_id,item_version,staging_key,sealed_key,bucket,etag,size,content_type,status,expires_at` |
| `item_locator` | `item_id` 主键、`tenant_id`；只供限定内部定位用途 |
| `reservations` | `claim_id` 主键、`tenant_id,item_id,item_version,state,created_at,released_at,committed_at,handover_id` |
| `import_jobs` | `id,tenant_id,created_by,state,total,accepted,failed,created_at` |
| `import_entries` | `job_id,line_number,client_reference,item_id,status,error_code`；唯一 `(job_id,client_reference)` |
| 公共表 | outbox、inbox、幂等、审计 |

所有员工业务表使用租户 RLS。`item_locator` 与 operational 表使用 01/02 的受限访问方式。`items.version` 是 ETag 及目录 `revision`；`item_version` 仅在影响 embedding 的内容变化时递增。

约束：标题 1—120 字符，描述 1—2000，分类 1—100；`found_at` 不能晚于服务端当前时间 5 分钟。核验问题 1—3 个，问题最多 300 字符，参考答案最多 1000 字符。答案禁止进入公开字段、日志和领域事件。

## 2. 状态与修改规则

| 当前状态 | 允许动作 | 新状态 |
| --- | --- | --- |
| `awaiting_upload` | 确认照片 | `processing` |
| `processing` | 当前版本 embedding 成功/终态失败 | `available` / `processing_failed` |
| `processing_failed` | 明确重试 | `processing` |
| `available` | 预留给某个 claim | `reserved` |
| `reserved` | 释放相同 claim 的预留 | `available` |
| `reserved` | 相同 claim 完成交接 | `handed_over` |
| `awaiting_upload/processing/processing_failed/available` | 撤销 | `withdrawn` |

终态物品不物理删除。`reserved/handed_over` 不允许修改照片、公开描述或核验题；预留释放后才允许修改。`withdrawn` 首期不恢复，重新登记使用新物品 ID。

修改照片/描述/分类时递增 `item_version`，使物品退出可搜索状态；若使用已封存照片则直接进入 `processing`，换照片则进入 `awaiting_upload`。旧 embedding 完成事件不能将新版本变为可用。每次公开状态或内容改变均产生 `item.catalog_changed` 完整快照。

对已有未审批认领，修改物品内容可能导致版本不一致；Claim 审批必须校验认领时的 `item_version`，不能悄悄采用新的核验资料。

## 3. 员工 API

员工写入均验证当前成员资格、租户、角色与场馆状态。

| 方法与路径 | 请求 | 响应/行为 |
| --- | --- | --- |
| `POST /v1/items` | `title,description,category,found_at,public_location_hint?,verification_questions[]` | 201，物品 ID、`awaiting_upload`、版本 |
| `GET /v1/items` | 分页、可选状态/分类 | 本场馆管理列表 |
| `GET /v1/items/{id}` | 无 | 本场馆管理详情；包含问题与参考答案，不包含失主联系方式 |
| `PATCH /v1/items/{id}` | 允许修改的字段、`If-Match` | 新版本及是否需要重新处理 |
| `POST /v1/items/{id}/withdraw` | `reason_code`、`If-Match` | 撤销并发布目录状态 |
| `POST /v1/items/{id}/uploads` | `content_type,size`、`If-Match` | 上传 ID、预签名上传参数、过期时间 |
| `POST /v1/items/{id}/uploads/{upload_id}/confirm` | 无；服务端读取对象信息 | 202，物品处理中状态 |
| `POST /v1/items/{id}/retry-embedding` | `If-Match` | 202，新处理操作；物品内容版本不变 |
| `POST /v1/item-imports` | `entries[]`，每批最多 100 条 | 202，批量任务 ID |
| `GET /v1/item-imports/{job_id}` | 分页 | 任务总览和逐项结果 |

`verification_questions` 输入为 `prompt,reference_answer`。详情与列表必须使用不同 DTO，列表不带隐藏答案。照片上传完成是独立动作，创建物品不自动生成登记事件。

上传方式首选预签名 POST，以限制对象前缀、文件大小和 Content-Type；允许 JPEG/PNG/WebP，单张最多 10 MiB，默认凭据 10 分钟有效。真正的图片解码、像素上限和格式校验由 worker 在 embedding 前执行，失败物品不可搜索。

## 4. 照片确认与不可变版本

客户端只可写 `staging/{tenant}/{item}/{upload_id}`，无权写最终封存前缀。确认时服务端校验上传记录归属、大小、类型、有效期及当前版本，不接受客户端提供任意 bucket/key。

先读取 staging 对象版本/ETag，再以条件复制或明确对象版本复制到唯一封存 key，例如 `sealed/{tenant}/{item}/{item_version}/{upload_id}`。若源对象在复制期间变化，确认失败并要求重试，不能用“HEAD 检查过”推定后续读取是同一份照片。

封存后只使用最终不可变 key 发布 `item.registered`。API/上传身份不具备覆盖封存对象的权限。重复确认复用相同封存结果及事件 ID；崩溃留下的未引用封存对象由宽限期清理任务处理，不影响已引用对象。

S3 操作放在数据库事务外，通过 `item_photos.status` 与条件更新记录进度。数据库确认成功的同一事务才转 `processing`、写 outbox 和幂等结果。设置至少 24 小时的 staging 清理宽限期，清理只针对无已确认引用的对象。

## 5. 内部 API

调用方必须通过 M2M 客户端白名单与 scope；路径资源定位得到的租户才是权威值。

| 方法与路径 | 调用者 | 契约 |
| --- | --- | --- |
| `GET /internal/v1/items/{id}/claim-context` | Claim；`items.context` | `item_id,tenant_id,item_version,state,title,questions[{id,prompt,version}]`，没有参考答案 |
| `GET /internal/v1/items/{id}/index-context` | Embedding/Matching；`items.index` | 当前公开快照、版本和封存照片引用；没有隐藏信息 |
| `POST /internal/v1/items/{id}/reservations` | Claim；`items.reserve` | `claim_id,item_version`；成功 200，冲突 409 |
| `GET /internal/v1/items/{id}/reservations/{claim_id}` | Claim；`items.reserve` | `held/released/committed` 与交接 ID；不存在返回 404 |
| `POST /internal/v1/items/{id}/reservations/{claim_id}/release` | Claim；`items.reserve` | `reason_code`；幂等释放或创建释放墓碑 |
| `POST /internal/v1/items/{id}/reservations/{claim_id}/commit-handover` | Claim；`items.reserve` | `handover_id,mode`；幂等提交交接 |

表中 scope 简写均加 `lostlink/` 前缀。API 响应完整携带资源版本。匹配结果对外照片访问必须经允许的公开快照接口生成短期只读 URL，不能向浏览器暴露内部服务凭据。

## 6. 预留并发与迟到请求

预留事务锁定物品行，验证 `state=available`、内容版本一致，并插入 `reservations`。相同 claim 已 held 则返回原结果；被其他 claim 持有则冲突。

预留不自动按短 TTL 释放，避免 Claim 已提交批准但 Item 自行过期。5 分钟仍没有完成审批的预留触发恢复检查及告警，由 Claim 协调器作出释放决定。

释放必须以 claim ID 条件更新，不能释放别人的预留。**即使预留还不存在，也要记录 `released` 墓碑**，阻止已超时的旧 reserve 请求稍后重新占用物品。同一 claim 的 released 状态不允许回到 held；重新认领使用新 claim ID。

交接只接受 held 状态和相同 claim，写入固定 `handover_id` 后进入 committed，同时物品变为 handed_over。相同交接 ID 重试返回成功，不同 ID 或 released 状态返回冲突。释放 committed 预留必须失败，不能让已交接物品重新可用。

每个状态变更和目录事件在同一数据库事务完成。

## 7. 批量导入

首期接口接收 JSON manifest，不接受任意远程图片 URL，不由服务端抓取外部网站。每条包含稳定的 `client_reference` 和普通登记字段；返回 item ID 后，客户端/seed 脚本执行各自上传及确认流程。

5,000 条以每批最多 100 条分批提交。导入工作者以小事务逐条处理，保存成功/失败及错误码；一条失败不回滚整批。同一批次的相同 `client_reference` 不重复创建物品。

重复导入请求使用同幂等 key 返回原 job；失败行通过新修复请求明确重试，不重新创建已经成功的条目。不得一次生成数千条未使用的预签名 URL。

## 8. 消费与依赖

消费 `item.embedded` 和 `item.embedding_failed`，验证事件租户与权威物品定位一致。未知物品、旧版本、状态已经撤销等情况记录受控处理结果，不能无条件覆写。worker 失败后人工重试必须产生新的逻辑处理事件 ID，避免 inbox 把新的重试当作旧消息。

显式重试虽然不改变内容版本，但必须更新 `processing_correlation_id`。登记及结果事件按 02 传递该轮次；只接受当前轮次的结果，迟到的上一轮成功或失败均正常确认但不改变状态。验收须包含“上一轮失败延迟到达，新一轮正在处理”的场景。

Matching 以目录快照控制可搜索状态。Item 不依赖 Matching 在线才能登记；Claim 的预留必须通过 Item 当前状态验证，不能仅信搜索缓存。

## 9. T2 验收与交付

- 同 key 创建/确认/导入重试不重复生成资源或逻辑事件。
- 照片未上传、对象不属于物品、错误大小/格式、复制期间源对象变化均不能变为可搜索。
- 两个 claim 并发预留同一物品只有一个成功；释放墓碑阻止迟到预留；重复交接保持单次结果。
- 已撤销/预留/交接物品不会被旧 embedding 事件恢复。
- 跨场馆管理、核验答案泄露、任意对象 key 和远程 URL 导入均被拒绝。
- 100 条批次中一条失败只影响该行；累计 5,000 条导入可查询进度并恢复。
- 提供迁移、OpenAPI、事件样例、内部客户端契约测试及 S3/数据库集成测试；不把 CLIP 算法开发计入本任务。
