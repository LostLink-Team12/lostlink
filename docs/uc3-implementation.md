# UC3 实现、验证与移交记录

实施日期：2026-10-20。分支：phase1-backend。起点为已提交的 UC2（ccb43fb）。本轮按用户要求继续完成报告与搜索，不自动 commit；当前改动供手动复查提交。

## 已完成范围

- 新增独立 matching_app 包、九张本领域表、冻结的 Alembic 初始迁移、vector(512) 和 HNSW cosine 索引。
- API/internal/worker 三类运行身份、FORCE RLS、事务内 owner/单资源上下文；禁止跨 schema 查询 Item。
- 失主报告创建、本人列表/详情、关闭与 ETag；创建/关闭幂等、审计、报告匹配任务与事务 outbox。
- 跨场馆文字/照片/融合搜索，分类、场馆与拾得时间过滤；搜索和匹配展示前通过实际 Item API 核验状态、版本、租户和处理轮次。
- 真实 OpenCLIP ViT-B/32 本地适配，完整权重 SHA256 校验、512 维归一化、多模态融合、版本隔离、图片解码和请求大小限制。
- item.registered/catalog_changed 的 SQS 消费、持久 inbox、封存照片读取与 CLIP 任务、item.embedded/embedding_failed 结果发件箱；沿用现有共享事件契约，不破坏原载荷。
- 有界 report_match、每页 50 报告的持久 item_match、report/item 唯一匹配、match.found 发件箱；失主先报失或物品先入库均可继续匹配。
- revision 墓碑、内容版本/correlation_id 防护、SKIP LOCKED 租约、任务恢复、指数退避、十次预算、非 embedding 任务与发件箱的手动重放审计。
- Notification 最小上下文接口，专用客户端 + lostlink/matches.notify；报告关闭、Item 不再可用或轮次改变时 active=false。
- HTTP 探针、指标、独立运行命令、模型下载校验脚本、ML 独立依赖锁、Docker 打包与根测试收集。

详细业务、架构与后续 Agent 约束见 [09：Matching/Embedding](backend-handoff/09-matching-and-embedding.md)，运行命令见 [服务 README](../services/matching-service/README.md)，成员接入事项见 [团队清单](team-alignment.md)。

## 实际模型与本地环境

本轮已安装 OpenCLIP 3.3.0、PyTorch 2.14.1、torchvision 0.29.1、Pillow 12.3.0；模型依赖记录在 requirements-ml.in/lock，共享 HTTP 服务依赖锁不变。

已从 OpenCLIP 官方预训练配置指向的 OpenAI 地址下载 ViT-B/32，完整摘要为：

```text
40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af
```

文件位于本工作区 .models/ViT-B-32.pt；Git 和 Docker 上下文均忽略该目录。接手者运行 `python scripts/prepare_clip_model.py` 可按固定地址重新准备，已有文件必须摘要正确，不覆盖未知文件。推理过程不访问模型下载站点。

测试使用 Windows 本地 CPU、Docker PostgreSQL/pgvector 和 LocalStack。没有修改已有开发数据库的业务 schema；集成测试创建随机数据库和六个受限运行角色，并在结束后清理。

模型实现参考 [OpenCLIP 官方仓库](https://github.com/mlfoundations/open_clip)，向量运算参考 [pgvector 官方仓库](https://github.com/pgvector/pgvector)。原始 OpenAI 文件是 TorchScript 归档，需要兼容加载；只加载运维配置并已校验摘要的可信文件。当前 PyTorch 会给出旧格式与 torch.jit.load 弃用提示，测试不屏蔽这些提示；这不是推理失败。

## 验证命令与证据边界

仓库根目录 PowerShell：

```powershell
$env:LOSTLINK_TEST_ADMIN_URL='postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink'
$env:LOSTLINK_TEST_AWS_ENDPOINT='http://localhost:4566'
$env:LOSTLINK_TEST_SCALE='1'
$env:CLIP_CHECKPOINT='C:\Workspace\GitHub\lostlink\.models\ViT-B-32.pt'
$env:CLIP_SHA256='40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af'
.\.venv\Scripts\python.exe -m pytest -q --tb=short
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
```

最终完整回归：**178 passed，124.33 秒**，没有 skip；包括 UC3 新增的 33 项测试、真实模型验收、LocalStack 和原有 5,000 条规模测试。六条提示均来自官方 TorchScript 归档的兼容加载和 PyTorch 弃用提示。Ruff 检查、119 个 Python 文件的格式检查以及 git diff --check 均通过。

实际覆盖：

| 证据 | 覆盖内容 | 限制 |
| --- | --- | --- |
| 真实 CLIP 测试 | 文字与照片同空间、512 维归一化、红/蓝样本排序、坏图拒绝 | 合成图片只证明链路与基本语义，不代表实际失物召回率 |
| 真实模型贯通 | 实际 Item API → 封存任务 → CLIP → pgvector → 照片搜索 → 报告匹配 | Item 照片存储和认证主体为测试注入，未使用真实 Cognito |
| SQL/RLS | 两场馆排序过滤、owner 隔离、internal 列权限、跨 schema 拒绝、索引、迁移往返 | 不等同于云端 RDS/IAM 验收 |
| 消息/恢复 | LocalStack SNS→SQS→事务 inbox、重复投递、outbox 往返、稳定 event_id、租约恢复与旧 token 拒绝 | 未在 AWS 执行故障注入 |
| 生命周期 | 报告先后到达、关闭后不重新匹配、53 报告跨页恢复、目录墓碑、旧轮次和终止失败、Item 故障关闭放行 | 邮件消费者仍待 UC4 |
| 原有规模测试 | 继续覆盖 UC2 的 5,000 条元数据登记和批次恢复 | 不能声称已计算 5,000 张照片或完成 CLIP 吞吐验收 |

基础 Docker 镜像 lostlink/matching-service:uc3 已构建并实际启动，容器 UID=1000，/health=200，缺配置时 /ready=503；测试容器使用 --rm，未占用宿主端口。ML Docker 变体尚未构建验收，真实模型测试运行在本地 Python CPU 环境。生产 API/任务推理镜像需 INSTALL_ML=true 和权重只读挂载，默认镜像仅支持轻量消费/outbox 等进程。

另已验证生产环境下 worker CLI 不依赖 API 的导入副作用；consume/outbox 不需要 API 数据库、Cognito 校验配置、Item 客户端密钥或本地模型配置。tasks 仍需独立 worker 数据库、Item M2M、照片 IAM 与模型。

## 对前序计划的落地调整

1. Matching 与 Embedding 在一个 schema 中拥有向量索引，进程独立；不将 proposal 中“独立 worker”解释成跨服务直接读写向量表。
2. 消费进程先提交持久任务再确认消息，CPU 任务积压位于 PostgreSQL；tasks 不能仅按 SQS 深度缩容至零。
3. Item 成功结果切 available 后的目录事件与向量完成均触发匹配，避免事件到达顺序造成漏匹配。
4. 通知上下文 venue_name 允许 null；现有 Venue 不提供机器可读公开名称目录，通知应使用通用标签，后续再补授权目录。
5. ML 依赖仅安装于推理工作负载；普通服务和基础 CI 不强制安装。真实模型测试只有配置 checkpoint 与依赖时才执行，缺配置会明确 skip。

## 后续任务

UC3 的本地后端报告与搜索已完成；UC4 的 SES/身份目录/发送去重，Claim 业务与真实云端接入仍独立待办。接手者还应根据实际物品数据标定阈值和多语言效果，并完成 5,000 张照片吞吐、CPU 镜像优化、重建索引工具与数据保留/清理策略。

本轮没有创建云端资源、真实发信、对外通知成员、commit、push 或 PR。所有对齐事项均保留为中文文档，未假设已被其他成员确认。
