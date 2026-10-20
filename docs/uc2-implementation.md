# UC2 实现、运行与后续接入

在 phase1-backend 按 [Item 原设计](backend-handoff/04-item-service.md)实现物品服务。基线为已提交的 UC1（ed6df6a），本轮不自动 commit。代码、配置、脚本和服务 README 使用英文，业务说明及对齐记录放在中文 docs。

## 已实现范围

- Item Service 独立包、12 张本服务表、冻结的初始 Alembic 迁移、强制 RLS、API/Internal/Worker 三类运行角色和独立 DDL 身份。
- 场馆物品登记、分页和筛选、详情、修改、撤销；核验资料只出现在许可员工详情，不进入公开快照、列表或事件。
- Venue 实时成员检查；旧令牌失去成员资格后不能继续访问 Item，依赖故障返回 503。
- 精确 key、类型、大小及有效期约束的 S3 POST；持久确认任务、不可变照片封存、孤立对象清理。
- 发件箱发布、SQS 结果消费、事务 inbox、处理版本与轮次保护、失败重放和审计。
- 每批最多 100 条的持久导入、逐行成功/失败、失败行修复、稳定批次去重；可运行的本地照片导入脚本。
- Claim 的上下文、预留、预留查询、释放墓碑和幂等交接提交；Matching/Embedding 的公开索引上下文。
- 探针、指标、类型化 OpenAPI、Dockerfile、根测试收集及 CI 的 S3 集成支持；沿用原有依赖锁，无额外运行依赖。

代码入口和启动命令见 [Item README](../services/item-service/README.md)。本轮不实现 CLIP 模型、Matching 搜索服务、Claim 业务流程或正式前端；这些服务的消费/调用契约已经提供。

## 对原设计的具体补充

### 照片确认分为受理与完成

确认接口返回 202 和 operation_id，表示任务已经入库。此时物品仍可处于 awaiting_upload；后台实际校验并封存成功后，才进入 processing、写 item.registered 与目录快照。前端查询 GET /v1/item-operations/{id} 区分完成、失败、过时和处理中，不将 202 当作可搜索。

上传申请本身更新 ETag；替换已有照片或待确认槽位会递增 item_version，并废弃先前槽位/处理轮次。确认只绑定服务端生成的 upload ID，不接收任意 bucket/key。未上传、大小或类型错误都不能触发 embedding。

上传凭据有效期仍为原始十分钟；同 key 重试复用同一上传资源，并只在原期限内重新生成预签名表单。签名及临时凭据不进入幂等响应快照。若任务受理后排队较久，允许处理在有效期内已经上传的对象；超过期限还没有上传则明确失败。

### 封存是条件读取与条件创建

后台先复用已存在、绑定相同上传 ID/版本/大小/类型的封存对象，支持 S3 成功但 SQL 提交前崩溃的恢复。否则按 HEAD 的 ETag/对象版本条件读取 staging，最多读取声明的图片大小，再以 If-None-Match 星号写入唯一 sealed key。源对象发生变化时失败/重试，已有封存对象不覆盖。

选择条件 GetObject + PutObject 是为明确约束源快照和目标创建；不依赖单次 HEAD 后无条件复制。S3 条件写及桶策略接入依据 [AWS 官方说明](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)。真实格式解码、像素上限和 CLIP 校验仍由 Embedding 在计算前完成；Item 只在收到匹配当前版本/轮次的成功结果后将其公开。

API 签名身份只能生成 staging 上传；封存写入由后台工作负载承担。云端须对 sealed 前缀限制写入者并强制条件创建。清理采用至少 24 小时宽限、分页和短事务，只删除确认无引用的孤立对象及可删除 staging，始终保留已确认 sealed 对象。未确认记录先标 purging，防止与人工恢复竞争。

### 内容版本、目录版本和处理轮次

- item_version 表示内容/核验版本；本实现对允许的内容 PATCH 统一递增，包括核验题、标题、地点和时间，以保证待审批认领不会继续使用旧资料。
- version 是 ETag，也是 item.catalog_changed 的 revision；状态变化也递增。
- processing_correlation_id 是一次逻辑处理的 UUID，登记和结果事件在信封中传递。相同内容的人工重试生成新轮次，传输重投保留旧轮次和事件 ID。

撤销、预留、交接或内容变化之后，旧成功/失败结果不能把物品恢复为 available。消费者还核验消息租户与单资源 locator 一致。同事件 ID 不同载荷视为协议错误，不确认删除；相同事件重投由 inbox 去重。只有数据库业务变更、回执及新发件箱同时提交后才确认 SQS。

### 批量任务与修复

POST /v1/item-imports 增加必需的 batch_reference，唯一约束作用于租户和批次。它与 manifest 摘要绑定，保证通用幂等记录到期后，同一批次仍不重复创建；同批次不同内容返回冲突。

外层校验条数、总大小及稳定 client_reference，单行内容由后台独立校验。没有远程抓图接口，photo_url 之类字段会使该行失败，不影响其他行。含隐藏答案的原始行保存在受租户保护的领域表，跨租户任务队列只保存行 ID。

GET /v1/item-imports/{id} 提供总计和逐项结果。POST /v1/item-imports/{id}/retry 接收失败 entry_id 及修复后的 item，不能重新导入成功行；用递增 generation 防止旧任务处理新数据。后台审计主体是 worker，物品 created_by 保留原请求人，二者不混淆。

scripts/import_items.py 从本地 manifest 目录读取照片，每 100 条提交一批，随后直传及确认。它会恢复相同批次，跳过已经处理的物品；若中断发生在待上传阶段，重跑申请新槽位，不重复创建物品。日志只输出计数，不输出 token、表单签名或核验内容。

### 预留和交接

内部调用同时检查 scope 与服务客户端白名单。通过仅对指定资源可见的 locator 得出租户，再开受限事务；内部角色在 SQL 层没有参考答案列的读取权限。

Claim ID 与物品行锁共同串行化 reserve/release/commit。释放尚不存在的预留也写 released 墓碑；released 不能回到 held。交接固定 handover_id，重复相同 ID/mode 返回原结果，其他 ID 或释放已交接预留返回冲突。

预留不按短 TTL 自动释放。五分钟后仍 held 则创建持久 needs_attention 状态和告警，由 Claim 恢复协调器查询并决定；Item 不自行猜测认领是否已经批准。

### 运行时钟及恢复

任务领取、租约和手动重放可用时间统一采用数据库时钟，避免 API 主机与数据库的细小时钟差使刚重放的任务暂时不可见。网络工作不占用 SQL 事务；迟到工作者通过 lease token 条件无法覆盖新工作者。

失败发件箱或依赖故障任务可以通过带审计的 worker CLI 重放。无效/过期照片需要新上传；导入的业务错误通过修复接口处理，不用运维重放重复创建。

## 接入清单

| 对接方 | 要接入的内容 |
| --- | --- |
| Yang Dongwei：Embedding | 消费 item.registered，读取 sealed 对象；成功/失败原样传递 correlation_id 和 item_version；实现实际图片校验与 CLIP，失败发布已注册的 item.embedding_failed |
| Yang Dongwei：Matching | 消费完整 item.catalog_changed，以 revision 去重，仅 available 可搜索，其他状态保留墓碑；结合向量和目录，使用 items.index 专用客户端 |
| Claim 后续 Agent | 使用 items.context/items.reserve 和固定幂等键；审批比较认领时的 item_version，恢复时查询预留，取消先确认释放，交接使用固定 handover_id |
| Lu Jingxing | 三类数据库角色/Secrets、私有 S3 桶及条件写策略、前端 POST CORS、SNS 事件过滤、结果队列/DLQ、四类独立 worker 入口与网关内部路径隔离 |
| Zhang Ji | access token、Idempotency-Key、If-Match、直传表单、operation 轮询和 processing/failed 状态；只在员工详情显示参考答案 |

Embedding 队列订阅过滤 item.registered；Item 结果队列只接收 item.embedded/item.embedding_failed；Matching 另订阅目录快照。不能让 Item 消费自己发出的全部目录事件。测试和 Compose 使用 LocalStack，真实 AWS 策略、Cognito、CLIP/Matching 联调仍需团队环境接入；未发送实际通知。

## 验证记录

本轮全量执行 python -m pytest -q，设置 LOSTLINK_TEST_ADMIN_URL、LOSTLINK_TEST_AWS_ENDPOINT 和 LOSTLINK_TEST_SCALE=1：145 项全部通过（105 项单元测试、40 项集成测试），其中包括 5,000 条分批登记及重复批次不增量、实际 Venue 权限撤销、S3 POST 上传中断恢复、封存后源对象覆盖、异常元数据、清理保护、SNS/SQS 投递和 inbox、预留并发/墓碑/交接、任务租约及审计重放。相较 UC1 新增 39 项测试。

Ruff 静态和格式检查通过，未新增依赖或改写锁定版本。规模验证区分 5,000 条数据库登记与少量真实图片上传；不将本地结果推断为真实 CLIP 吞吐、AWS p95 或云端扩缩容。

最终 Linux 镜像 lostlink/item-service:uc2 构建通过。临时容器以 UID 1000 运行，pip check 无依赖问题；真实 HTTP 核验 /health=200、缺少配置时 /ready=503、未登录物品列表=401，并读取包含 18 个路径的 OpenAPI。临时容器已自动删除，没有常驻额外端口。未执行真实 AWS 资源变更、发信或外部通知。
