# Issue #7：搜索 p95 验证与 Item 逐候选查询调查

本方案对应 [Issue #7](https://github.com/LostLink-Team12/lostlink/issues/7) 的要求：
Matching 搜索同步逐条调用 Item，用 k6 采样 p95；超过 **500 ms** 时调查并决定是否增加
batch internal API 或短缓存。这里交付验证工具、可复现环境及判读规则。
真实 AWS dev 验收必须在部署环境另跑，不能用本地 fixture 或 CI 控制服务器替代。

Issue #10 的 200 VU 搜索、5,000 张图片导入、HPA/KEDA 展示仍由其 scaling demo 负责。
原有 `smoke.js` 保持一位用户、五次查询和 5 秒 smoke 门槛。

## 1. 测什么，如何避免“很快但没有结果”

入口是 `tests/load/search_review.js`，只发送 `POST /v1/search`，不创建报告、不上传照片。
`search_review_contract.js` 共用配置、响应和报告校验。

每个用例必须给出公开的名称、查询、明确的 `limit`、至少一个已知应命中的物品 UUID。
可另给 `forbidden_item_ids`，断言已撤回、版本过期等已知不应返回的物品没有泄露。
支持 text、photo 和 fused；筛选字段包括 category、venue、时间范围和 min_score。
图片使用原始 base64，不能传 data URL、物品 URL 或预计算向量。
CLIP score 是余弦相似度，不是物品身份的概率；这里验证检索和接口行为，不评估识别准确率。

每个用例单独执行预热，预热必须得到有效结果，否则停止，不开始测量。
正式阶段使用少量固定 VU，轮询用例；每个用例分别统计并判定：

| 条件 | 要求 |
| --- | --- |
| HTTP / 响应契约 | 全部 HTTP 200；JSON 字段、UUID、版本、分数、过滤条件有效 |
| 结果正确性 | 已知应命中的 ID 全部出现；禁止 ID、重复 ID 不出现；结果不少于 `min_results` |
| 配置一致 | 返回 `candidate_limit` 等于预期；可固定 `model_version` |
| 样本 | 每个用例达到最低样本数，不能把三类查询加总后作为某一类的样本数 |
| 延迟 | 每个用例 p95 **≤ 500 ms**，任意一类失败则整轮失败 |

`limit` 是返回数量（1–100）；`candidate_limit` 是服务端候选上限（20–100），不是请求参数，
也不是这次实际返回或校验的候选数。只修改 k6 的预期值不会改变服务端设置。
因此不能仅看返回一条结果就推断只调用了一次 Item。

门槛使用 `response.timings.duration`，即 k6 HTTP 请求的发送、等待、接收耗时；
不包含建连和 TLS 握手。另记录 `wall_p95_ms`，涵盖客户端调用和解析/校验耗时，辅助查看差异。
两者均不包括轮询间 10 ms 的停顿。预热产生的内置 HTTP 指标不用于本方案的正式门槛。
详见 [k6 metrics](https://grafana.com/docs/k6/latest/using-k6/metrics/)、
[thresholds](https://grafana.com/docs/k6/latest/using-k6/thresholds/)。

## 2. 两种运行档位与输出

| 环境变量 | 默认值 / 范围 |
| --- | --- |
| `MATCHING_API_URL` | 必填，固定 HTTPS 服务地址；无凭据、query、fragment |
| `LOSTLINK_ACCESS_TOKEN` | 必填，通过本机会话设置的 Owner access token |
| `SEARCH_CASES_FILE` | 必填，用例 JSON 文件路径 |
| `SEARCH_SUMMARY_FILE` | 可选，聚合 JSON 输出路径；父目录应先建好 |
| `SEARCH_PROFILE` | `measurement` 或 `validation` |
| `SEARCH_DURATION` | measurement 默认 `60s`、至少 `10s`；validation 默认 `5s`、至少 `1s`；最多 `60m` |
| `SEARCH_MIN_SAMPLES` | measurement 默认且至少 100/用例；validation 默认 5，最低 1 |
| `SEARCH_VUS` | 默认 1，范围 1–20；此上限用于限制 backend 调查范围 |
| `SEARCH_WARMUP` | 默认每用例 5 次，范围 1–100 |
| `EXPECTED_CANDIDATE_LIMIT` | 默认 100，范围 20–100，须与服务端一致 |
| `EXPECTED_MODEL_VERSION` | 可选，须与服务器实际模型版本完全相等 |
| `SEARCH_SCOPE` | 默认 `dev`；`local-fixture` 才允许明文 loopback HTTP |

measurement 的 100 个样本是工具的最低门槛，不代表充分的统计置信度。
正式报告建议每格至少 1,000 个样本、稳定运行至少 5 分钟、独立重复三轮。
若时间用尽但采样不足，延长测试，不用增大 VU 来“凑样本”后与原来的并发比较。
这是 closed-model 调查，不能据此声称能承受某个固定 RPS 或推断最大容量。

脚本打印聚合 JSON，可同时写文件。按用例保留样本数、有效率、p95/p99、wall p95、
结果数量范围和决定，不包含 token、查询文字、照片、物品/租户 ID 或响应正文。
用例名称是公开报告标签，应使用 `text-common` 这类名称，不放个人信息。
不要额外启用 HTTP debug、原始 JSON 输出或外部 metrics exporter。
用例源文件可能包含私有查询/图片，存入被 Git 忽略的 `build/`，只分享聚合报告。

| `decision` | 含义 / 下一步 |
| --- | --- |
| `insufficient-samples` | 样本不足，不能形成延迟结论；先延长测量 |
| `invalid-responses` | 先修复身份、服务、fixture 或检索正确性；快速失败不能算性能通过 |
| `investigate-latency` | 有效且足量，但 p95 超门槛；按第 5 节定位 |
| `within-500ms-for-this-run` | 仅本轮、该数据、该并发达到门槛 |

k6 任何门槛失败均非零退出。`passed` 表示本轮全部用例通过；
`acceptance_eligible` 只有 dev + measurement + 全部通过才为 true，表示可以纳入人工验收证据。
它不能自动证明目标确实是 AWS、数据具有代表性或部署配置正确。
validation 和 local-fixture 即使 `passed: true`，也始终不能作为 AWS 验收结论。

## 3. 本地复现：真实 CLIP、pgvector、两个 HTTP 服务

先准备 Python 3.12+、Node 24、Docker。以下从仓库根目录、已建立的 `.venv` 运行：

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-dev.lock -e ./libs
.venv/Scripts/python.exe -m pip install -r services/matching-service/requirements-ml.lock
docker compose up -d postgres
.venv/Scripts/python.exe scripts/prepare_k6.py
.venv/Scripts/python.exe scripts/prepare_clip_model.py
node --test tests/load/search_review.test.mjs

# 仅 docker-compose.yml 的本地开发数据库凭据。不要填 AWS/prod 地址。
$env:LOSTLINK_TEST_ADMIN_URL = 'postgresql+psycopg://lostlink:lostlink@127.0.0.1:5432/lostlink'
$clipSha = '40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af'
.venv/Scripts/python.exe scripts/run_search_review_local.py `
  --checkpoint .models/ViT-B-32.pt --sha256 $clipSha `
  --candidates 20 --duration 60s --output build/search-review/local-twenty
```

`prepare_k6.py` 下载官方 k6 v1.4.0，核对固定发行 SHA256，只放入 `build/tools/k6/`；
支持 Windows/Linux amd64。其他平台可自行安装 k6，通过 `--k6` 指定可执行文件。
校验值来源：[官方 v1.4.0 release](https://github.com/grafana/k6/releases/tag/v1.4.0)。

本地 runner 会：

1. 检查模型 SHA，用真实 OpenCLIP ViT-B/32 CPU 推理；默认 OMP/MKL 各 2 个线程，已有线程配置会保留。
2. 只接受 loopback PostgreSQL URL，新建随机 `ll_search_review_*` 数据库。
3. 调用第二步的 `backend_admin.py`，创建六个独立 runtime role、迁移 Item/Matching；
   请求使用受限角色执行事务/RLS，管理员连接仅用于建立、造数和清理环境。
4. 直接种入 1、20 或 100 条候选，向量来自真实 CLIP。同一向量产生相同距离，按 UUID 排序；
   Matching 索引仍认为全部 available，而 Item 中只有最后一条 available。
   `limit=1,min_score=-1` 强迫服务逐条校验到末尾，同时断言撤回物品没有返回。
5. 启动真正的 Item 与 Matching Uvicorn HTTP 服务，只监听 `127.0.0.1` 随机端口。
   通过现有 app factory 注入随机测试身份和 loopback Item transport，不给生产入口增加认证开关。
6. 运行 k6，记录每请求 CLIP、Matching 数据库事务、Item 校验总耗时及实际 Item 调用数。
   自动确认每次实际调用数确为候选数；随后停服、释放连接，删除本轮数据库和六个角色。

不使用 Cognito、TLS、S3、事件、worker 或入库流程；不是端到端图片导入，也不评估模型召回。
HTTP transport 使用 httpx 连接复用，与生产 Item client 的 HTTPS/建连行为不同。
CPU、Docker、计时包装也有本地开销，因此只能用于发现调用放大和验证工具。
分段 p95 是各分段的分布，不能直接相加等同总请求 p95。
正式阶段的分段统计与预热分开，但三种模式一起运行时分段统计合并；
定位某种模式时单独 `--modes text|photo|fused` 运行。

`--output` 必须是不存在的新目录，防止复用旧 summary。输出含用例与 `summary.json`，不写 token。
正常退出、失败及普通中断均执行清理；机器断电/强制杀进程时，管理员可检查并清理遗留的
`ll_search_review_*` 数据库和 `ll_sr_*` 角色，先核实名称，不能删除正常服务资源。

建议分别运行以下矩阵，不要同时跑，避免 CPU/数据库资源互相干扰：

| 场景 | 参数 / 目的 |
| --- | --- |
| 1 条候选 | `--candidates 1 --duration 20s`，基础耗时；实际 bound 是 20，实际 Item 调用应为 1 |
| 20 条候选 | `--candidates 20 --duration 60s`，默认小候选集的逐条校验放大 |
| 100 条候选 | `--candidates 100 --duration 180s`，候选上限及最迟有效物品；慢时继续延长以满 100 样本 |
| 三种模态冒烟 | `--candidates 20 --modes text,photo,fused --profile validation --duration 15s --min-samples 5` |
| 人工延迟诊断 | `--candidates 20 --item-delay-ms 30 --profile validation --duration 10s --min-samples 5`，验证顺序放大和失败门槛 |

每格指定不同输出目录。人工延迟会在报告明确标记，不能把它写成真实网络性能缺陷。
全部候选只有最后一个有效是可控的最坏路径，不能当成真实业务中撤回比例。

## 4. AWS dev 的只读执行步骤

AWS dev 部署、Owner 登录及数据就绪后，可直接使用同一 k6 脚本，不启动本地 runner。

1. 记录被测 commit/image digest、机器/Pod CPU 内存、CLIP device/thread 配置、模型 SHA、
   API/Item Pod 副本、数据库规格/版本、测试机位置和时间窗口。保留固定副本的基线；
   需要比较 HPA 时另开一轮，不能混用两个配置的样本。
2. 用正常 Item 导入和 embedding 流程准备非敏感 dev 数据，等物品 available、索引一致。
   选出 text/photo/fused 都有已知命中的查询；记录可用的物品 ID、租户、分类及时间范围。
   可用 `tests/data` 已有数据工具准备数据，但本 k6 脚本不会帮忙导入。
3. 复制 `tests/load/search_review.cases.example.json` 到 `build/search-review/dev-cases.json`，
   替换占位 ID、文字和过滤条件。每个用例至少一个应命中物品，结果上限足以容纳预期 ID。
   准备 photo/fused 时，将已核查的本地图片编码为 base64 写入 `payload.photo_base64`；
   fused 同时保留 `text`。不要把示例红色背包当作已存在的测试数据。
4. 在测试身份会话中取得短期 Owner access token，确保有效期覆盖预热及测量。
   先核对 Matching/Item readiness、Cognito scopes/allowlists、依赖与模型，避免把配置错误解释成性能问题。
5. 起点用 VU=1、固定配置、text 单用例至少 5 分钟；随后分别测 photo/fused。
   对同一数据、模型，比较 VU=5、20；按需延长时长满足每用例样本数。
6. 服务端分别设置候选上限 20、50、100 并重启/部署，确认响应中的实际配置。
   另比较返回 `limit=1/20/100`、常见过滤/稀疏过滤、通常首个即有效/末尾才有效路径。
   受控撤回数据要等正常索引同步再测稳态；模拟索引滞后另行标记。
   返回 candidate_limit 只是上限，真实逐条调用数须结合 tracing/剖析或受控 fixture 确认。
7. 同时保存 HTTP/数据库错误率、CPU/内存、连接池、Item 延迟、队列积压和扩缩容记录。
   将聚合 summary 与上述无密钥配置快照归档；不能只截一张“平均延迟”图作为验收。

```powershell
New-Item -ItemType Directory -Force build/search-review | Out-Null
$env:MATCHING_API_URL = 'https://DEV-MATCHING-API'
# 通过本地登录/密钥会话设置 LOSTLINK_ACCESS_TOKEN；不要写入文件、命令历史或 PR。
$env:SEARCH_SCOPE = 'dev'
$env:SEARCH_PROFILE = 'measurement'
$env:SEARCH_CASES_FILE = (Resolve-Path build/search-review/dev-cases.json).Path
$env:SEARCH_SUMMARY_FILE = Join-Path (Resolve-Path build/search-review).Path 'dev-text-vu1.json'
$env:SEARCH_DURATION = '5m'
$env:SEARCH_MIN_SAMPLES = '1000'
$env:SEARCH_VUS = '1'
$env:SEARCH_WARMUP = '5'
$env:EXPECTED_CANDIDATE_LIMIT = '100'
# 可选：EXPECTED_MODEL_VERSION，固定为这次部署的实际模型版本。
build/tools/k6/k6.exe run --no-usage-report tests/load/search_review.js
# 查看退出码；PowerShell 为 $LASTEXITCODE。用唯一 summary 文件名保留每轮证据。
```

可复现性要求：清除旧的 `K6_*` overrides，不传 `--no-thresholds`、不覆盖 scenarios，
运行前核对全部 SEARCH_* 参数；更换用例时不要残留其他测试的配置。
summary 只含允许分享的聚合数据；原始用例、token 和查询响应不应作为 CI/PR 附件。
本仓库的 CI 不连接 AWS dev，也没有自动收集真实 Cognito token。

## 5. p95 超过 500 ms 后如何决定优化

先检查结果有效率和样本数。失败或不足时先解决这些问题。
对可复现的有效慢请求，在相同数据、模型、负载下分别定位：

- **CLIP 占主导**：对比 text/photo/fused，检查 CPU 限额、线程竞争、推理锁、GPU/模型预热。
  缩短 Item 查询并不能消除推理排队，应先测量真正瓶颈。
- **向量/数据库占主导**：检查 pgvector 执行计划、索引、过滤选择性、数据库/连接池等待，
  同时确认不是客户端漏传过滤器或部署使用了管理员凭据。
- **Item 校验占主导，耗时随实际候选数增长**：优先评估只接受受限 ID 列表的 batch internal API，
  保留每条的租户、版本、correlation 和 state 校验、scope/client 限制及缺失条目的 fail-closed 行为。
- **重复 Item 校验占主导**：可评估有明确上限、短 TTL 的缓存，但要定义撤回/预留/交付/换图后如何失效。
  不能仅为了速度复用旧的 available 状态，导致已不可认领物品被当作当前有效。

云端没有本地的计时包装；若现有 HTTP 指标不足以区分分段，先在 dev 采集对应 HTTP spans/剖析证据，
不要从总 p95 武断归因于 Item。优化应另行实现和评审，再用原矩阵重跑：
正确性门槛不变、样本充分、逐用例 p95 达标，并核对实际调用数/资源占用变化。

## 6. 工具自身的验证与本次执行记录

```powershell
node --test tests/load/search_review.test.mjs
$env:LOSTLINK_TEST_K6 = (Resolve-Path build/tools/k6/k6.exe).Path
.venv/Scripts/python.exe -m unittest discover -s tests/load -p test_search_review.py -v
```

Node 用例覆盖配置、模态、过滤、完整契约、ID 断言和脱敏报告；Python 控制服务器驱动真实 k6，
验证正常、预热排除、空结果、错误 ID、错误 bound、503、慢响应、样本不足、预热失败退出。
`.github/workflows/search-review.yml` 在 Linux 上运行这些检查，无 AWS token、数据库或模型下载。
这些控制响应只能证明门槛有效，不能证明应用达到 500 ms。

2026-10-22 本地实测：Windows 11 amd64、24 个逻辑 CPU、CLIP CPU（OMP/MKL 各 2 线程），
k6 v1.4.0，pgvector/pg16 Docker；服务代码基线 `662e03c`，测试工具为本分支未提交改动。
模型 SHA 为上文固定的 `40d365…50af`。下表均为 VU=1、text、无人工延迟、每轮预热两次，
只有最后一条候选仍 available 的受控数据；每轮全部响应有效、结果恒为一条。

| 实际候选 / 每请求实际 Item 调用 | 正式样本 | p95 (ms) | p99 (ms) | Item 校验总耗时均值 (ms/请求) | 该轮门槛 |
| --- | --- | --- | --- | --- | --- |
| 1 / 1（服务 bound=20），20 秒 | 245 | 93.22 | 103.26 | 11.96 | 通过 |
| 20 / 20，60 秒 | 204 | 386.75 | 438.67 | 218.71 | 通过 |
| 100 / 100，180 秒 | 154 | 1497.52 | 1614.87 | 1086.75 | **未达标：investigate-latency，非零退出** |

100 候选的正式请求中，CLIP 均值约 55.76 ms，Matching 数据库事务均值约 19.86 ms，
逐条 Item 校验均值约 1086.75 ms。受控场景中调用放大是主要耗时来源，应将 batch internal API
列为下一步优先评估项；短缓存仍须先论证状态一致性。此结果不证明真实业务中有 99% 索引滞后，
也不证明 AWS dev 必然同样超标。

聚合证据分别位于被忽略的 `build/search-review/baseline-one/summary.json`、
`candidates-twenty/summary.json`、`candidates-hundred/summary.json`。
本次未执行 AWS dev 验收，所有本地报告均为 `acceptance_eligible: false`。

另执行三种模态的 validation 档位：text/photo/fused 分别取得 17/17/16 个有效样本，
每请求确认 20 次 Item 调用，撤回物品均未返回。人工延迟 validation（每次 Item 调用额外 30 ms）
取得 11 个有效样本、p95 约 964.80 ms，按预期触发非零退出；这只验证延迟开关和门槛，
不作为自然网络延迟结论。报告在 `three-modalities/summary.json` 和 `injected-delay/summary.json`。

本次 6 项 Node 契约测试、5 项真实 k6 控制测试、155 项 shared-library/Matching 单元回归通过；
Ruff 与 OpenAPI 导出检查通过。GitHub Actions workflow 已配置，尚未推送运行。
