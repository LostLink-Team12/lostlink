# Cognito V2 令牌触发器：打包与部署交接

对应 Issue #7。本步骤只准备 Lambda ZIP、入口验证和部署输入；不创建 AWS 资源、不绑定真实用户池，也不表示真实登录已验收。

## 1. 生成部署产物

在仓库根目录运行，需 Python 3.12+、Docker Desktop 的 Linux 引擎及 Buildx：

```powershell
python scripts/build_cognito_trigger.py
```

默认输出（均在 Git 忽略的 `build/` 中）：

- `cognito-token-trigger.zip`：Python 3.12、**x86_64** Lambda 部署包。
- `cognito-token-trigger.zip.sha256`：ZIP 校验值。
- `cognito-token-trigger.manifest.json`：运行时、架构、handler、锁文件/证书/ZIP 摘要和解压大小。

脚本使用 `services/venue-service/Dockerfile.lambda`，在 AWS Python 3.12 Linux 运行时安装根目录 `requirements.lock` 中的完整固定版本依赖，包含 Boto3，避免与运行时自带 SDK 混用。导出 `venue_app`、`lostlink_common` 和依赖到 ZIP 根目录；不使用 Windows `.venv`，不收集 `.env`、测试账号或数据库密码。文件权限为 0644，解压大小必须小于等于 250 MiB。依赖和源码不含部署配置。ZIP 中另附从实际 Linux 安装生成的 `requirements.txt`，供 Trivy 识别依赖；仅扫描 `.dist-info` 目录会漏掉此类依赖扫描。

RDS 证书从 AWS 的公开 [global bundle](https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem) 获取，验证可解析后打包为 `rds-global-bundle.pem`。可指定已审阅的本地 PEM：

```powershell
python scripts/build_cognito_trigger.py --rds-ca C:/certificates/global-bundle.pem
```

基础镜像使用 AWS 的 `3.12` 更新标签，证书也会轮换；重建可能得到不同的字节。部署时保留并核对该次 ZIP 的摘要，不把锁定依赖版本等同于永久冻结基础镜像或证书。

## 2. 验证最终 ZIP 和真实入口

先启动 Compose PostgreSQL，并安装后端开发依赖。然后运行：

```powershell
$env:LOSTLINK_TEST_ADMIN_URL='postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink'
$env:LOSTLINK_TEST_TRIGGER_ARTIFACT=(Resolve-Path build/cognito-token-trigger.zip).Path
python -m pytest -q services/venue-service/tests/test_venue_integration.py -k token_trigger
```

每个场景有两个执行路径：本机直接调用 `venue_app.token_trigger.handler`，以及将最终 ZIP 复制到 AWS Python 3.12 容器内解压，由 Lambda Runtime Interface Emulator 调用相同 handler。容器不挂载源码仓库、不使用本机 Python 依赖。测试核对 ZIP/证书摘要、架构、布局和大小；容器通过 `host.docker.internal` 访问本机已发布的 PostgreSQL 端口（Linux CI 显式配置 host-gateway）。

测试创建并清理随机命名的数据库和四个独立受限角色，不复用真实用户池或线上数据库。覆盖：

- 有效管理员、员工和无场馆失主，以及平台应用客户端。
- 同一 warm 进程轮流处理不同用户/场馆，不沿用上一用户的数据库访问上下文。
- 员工晋升、停用和场馆停用后，下一次调用立即反映数据库最新状态。
- 旧事件版本、错误用户池和未允许客户端被拒绝。
- 数据库锁导致查询超时、SELECT 权限撤销时不返回旧身份；恢复后同一进程可再次成功。
- M2M 事件原样返回，即使成员表被锁，也不会查询人类成员目录。

未设置 `LOSTLINK_TEST_TRIGGER_ARTIFACT` 时，只跳过 ZIP 路径；本机入口仍可测试。`.github/workflows/cognito-trigger.yml` 则总是构建 ZIP、运行两条路径、扫描解压后的依赖，全部通过后上传带提交 SHA 的 Actions artifact。该工作流不申请 AWS 写权限，也不自动部署。

入口失败时仅记录固定消息和异常类型，再抛出统一的 `Token customization unavailable`，不把 SQL 参数、用户事件或配置值交给 Lambda 的默认异常日志。不会用已缓存的旧角色作为失败兜底。

## 3. 部署方需要提供的配置

| 项目 | 要求 |
| --- | --- |
| Lambda | ZIP 类型、`python3.12`、`x86_64`；handler 为 `venue_app.token_trigger.handler`，不是 Uvicorn/API 启动命令 |
| 用户池 | 同区域的真实池 ID；支持 access token 自定义的功能计划；pre-token generation 版本明确为 **V2_0** |
| 数据库 | Venue 迁移已完成；独立 identity 登录角色，只读当前 subject 的成员数据；不注入迁移/API/platform 凭据 |
| 网络 | Lambda 加入可访问 RDS 的 VPC/子网；数据库安全组须明确允许 **Lambda 安全组** 的 5432 流量，不能只允许 EKS 节点 |
| 执行角色 | 日志和 VPC ENI 所需 IAM 权限；若部署系统从秘密管理器注入配置，按其方式授予权限。本 handler 本身不调用 Cognito、SES 或秘密管理 API |
| 调用权限 | Lambda resource policy 允许 `cognito-idp.amazonaws.com` 调用，限定实际用户池 `SourceArn` 和账号；不授予任意用户池调用 |

环境变量由部署秘密配置注入，不提交带真实值的文件：

| 环境变量 | 内容 |
| --- | --- |
| `ENVIRONMENT` | `dev` 或 `prod` |
| `IDENTITY_DATABASE_URL` | `postgresql+psycopg://ROLE:ENCODED_PASSWORD@RDS_HOST:5432/DB?sslmode=verify-full&sslrootcert=/var/task/rds-global-bundle.pem`；密码按 URL 编码 |
| `COGNITO_USER_POOL_ID` | 真实用户池 ID |
| `USER_CLIENT_IDS` | Owner/Venue 人类客户端 ID 的 JSON 数组，如 `["owner-client","venue-client"]` |
| `PLATFORM_CLIENT_IDS` | 运营客户端 ID 的 JSON 数组；没有此入口时可为空数组 |
| `DB_POOL_SIZE` | 建议初始值 `1` |
| `DB_MAX_OVERFLOW` | 建议初始值 `0` |
| `DB_STATEMENT_TIMEOUT_MS` | 建议初始值 `500`，在 dev 按数据库延迟验证后调整 |

触发器仅从环境读取配置，不读取 `.env`，无需 `DATABASE_URL`、`COGNITO_ISSUER`、M2M secret 或 AWS 静态访问密钥。若额外设置 `COGNITO_CLIENT_IDS`，仍须符合 Settings 对完整客户端集合的校验。

建议先用 512 MiB 内存、5 秒 Lambda timeout，在 dev 测量冷/热启动并调整；这只是初始配置。Cognito 同步触发器有固定的 5 秒响应窗口，不能通过单纯增大 Lambda timeout 延长。当前数据库连接超时为 3 秒；应结合 500 ms SQL 超时、网络连接和冷启动验证整体预算。用保留并发限制数据库连接上界；每个 warm 进程缓存连接池配置，因此轮换身份凭据后需部署新版本/刷新执行环境。

将审核过的 ZIP 部署为 Lambda 新版本，再绑定用户池；更新用户池时保留已有其他触发器和配置，通过负责该资源的 IaC 修改，避免整段覆盖。这里不提供会直接覆盖团队用户池的命令。此步骤不包含 M2M resource server/scope 的创建。

## 4. 云端验收边界

本地运行时模拟器验证了 Linux 原生依赖和入口行为，不能替代真实 Cognito 的签名令牌、VPC/IAM、RDS TLS、功能计划或冷启动时限验收。

- [ ] 实际绑定为 V2_0；Owner/Venue/运营客户端均能登录。
- [ ] 验证 access token 的 `cognito:groups` 和 `custom:tenant_id`，拒绝将 ID token 用作 API access token。
- [ ] 员工或场馆停用后刷新登录，确认旧角色被移除；已有 access token 的敏感写入仍走实时成员校验。
- [ ] 冷/热调用均在 Cognito 时间窗口内；数据库不可用时登录失败，不返回旧角色。
- [ ] 云端 RDS 使用 `verify-full`；真实日志不包含事件、令牌、密码或连接字符串。
- [ ] 保存版本、ZIP SHA256、用户池 ID、验收时间及结果；再将 Issue #7 对应项标为完成。

参考：[AWS Python ZIP 打包](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html)、[Cognito pre-token V2](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-lambda-pre-token-generation.html)、[同步触发器时限](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-working-with-lambda-triggers.html)、[RDS TLS 证书](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html)。
