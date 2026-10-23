# 数据库角色、迁移和密钥预检工具

对应 Issue #7 的运行角色、迁移和 Claim 密钥准备。入口为 `scripts/backend_admin.py`，配置样例为 [`infra/database/backend.example.json`](../infra/database/backend.example.json)。本工具只操作指定 PostgreSQL 数据库或读取本进程环境；不创建 AWS 资源、不写秘密管理器、不更换密钥、不执行降级迁移。

## 1. 命令与边界

| 命令 | 行为 | 是否写数据库 |
| --- | --- | --- |
| `plan` | 校验公开配置，列出目标库、服务、角色和需要的环境变量名；无需凭据或数据库连接 | 否 |
| `bootstrap` | 校验已有角色及其密码；原子创建缺少的受限运行角色，授予目标库 CONNECT；包含 Matching 时确保 pgvector 在 public schema | 是 |
| `migrate` | 先检查全部所选服务的运行角色及密码，再按 Venue → Item → Matching → Claim → Notification 执行现有 Alembic `upgrade head` | 是 |
| `check` | 核对迁移版本、表和 FORCE RLS、角色属性/成员关系/所有权、关键授权、跨服务隔离、运行凭据和 pgvector | 否，只读事务 |
| `keys` | 离线校验 Claim 字段加密 keyring、活动 ID、交接 HMAC key，并执行临时加解密自检 | 否，不连接数据库 |

所有命令输出一条 JSON，成功退出 0，失败退出 1。失败只输出固定错误码或异常类型，不打印密码、连接字符串、配置输入、密钥、SQL 参数或迁移子进程的原始输出。

`--service venue` 等可选择单个服务，也可重复该参数。未指定时处理五个业务服务的 **15 个运行角色**。Delivery 和 service-template 不在当前范围内。

## 2. 配置与凭据

先安装已有后端开发依赖：

```powershell
python -m pip install -r requirements-dev.lock -e ./libs
python scripts/backend_admin.py plan
```

样例只含公开配置：目标数据库名、每个服务迁移 URL 的环境变量名、运行角色名、各密码的环境变量名。**没有密码默认值**，也不读取 `.env`。若使用自定义名称，可复制到 `build/backend.config.json` 等本地路径，后续各命令使用 `--config build/backend.config.json`。不得将真实 URL、密码或密钥填入 JSON；未知字段会被拒绝。

执行前通过当前秘密会话注入：

- `BOOTSTRAP_DATABASE_URL`：一次性角色初始化操作身份，须能管理角色、授予目标库 CONNECT；若需安装 pgvector，还须有安装该扩展的权限。仅 `bootstrap` 读取它。
- 五个 `*_MIGRATION_DATABASE_URL`：各服务现有迁移身份的连接 URL。它们必须与运行身份分开，并有创建自身 schema、表和授权的权限；可以按团队部署方案使用一个受控迁移操作身份，或各服务独立身份。工具不会创建或扩大迁移身份权限。`migrate`/`check` 不需要初始化管理员 URL。
- 15 个 `*_DB_PASSWORD`：见 `plan` 的 `password_env`。每个运行角色使用独立密码，至少 16 个字符，不含 NUL；所选角色之间、与迁移/初始化密码之间不能复用。已有角色必须提供其**当前密码**。

运行连接由该服务迁移 URL 的主机、端口、数据库和 TLS 参数构造，仅替换为对应运行用户名和密码。因此不需要再提供 15 条重复 URL，也不会将迁移密码用于运行连接。密码中的引号、空格、百分号、反斜线或 `@` 等字符由驱动参数处理，不拼接到 SQL 标识符中。

数据库 URL 必须使用 `postgresql+psycopg://`，库名须与公开配置一致。只接受明确的 TLS/应用名相关查询参数，拒绝用 `user`、`dbname`、`options` 等参数绕过已验证的目标或身份。初始化和迁移 URL 必须采用相同主机和端口；本工具针对当前项目的单库、多 schema 架构。

默认 `--environment dev`，`dev`/`prod` 均要求 URL 显式包含：

```text
?sslmode=verify-full&sslrootcert=/path/to/reviewed-rds-ca.pem
```

证书路径须存在；也接受 libpq 支持的 `sslrootcert=system`，实际证书验证仍由连接驱动执行。密码需按连接 URL 的规则编码。RDS 私网连接、IAM/网络访问和证书信任必须由部署环境提供；工具不会绕过这些条件。

## 3. 执行与重复运行

注入秘密后，从仓库根目录按以下顺序运行：

```powershell
python scripts/backend_admin.py plan --config build/backend.config.json
python scripts/backend_admin.py bootstrap --config build/backend.config.json
python scripts/backend_admin.py migrate --config build/backend.config.json
python scripts/backend_admin.py check --config build/backend.config.json
python scripts/backend_admin.py keys
```

每一步仅在上一条退出 0 后继续。`plan` 本身不会检查秘密是否就绪，不代表可以部署。

本地 Compose 另加 `--environment local`，且目标主机必须为 `localhost`、`127.0.0.1` 或 `::1`。该模式允许不启用 TLS；不要将它用于云端。`scripts/init-local.sql` 的固定开发密码不适用于这个部署工具。

### 初始化的重复执行约定

- 新角色为 LOGIN、NOSUPERUSER、NOCREATEDB、NOCREATEROLE、NOINHERIT、NOREPLICATION、NOBYPASSRLS，不获得迁移/表所有权。
- 已有角色的密码保持不变。密码不匹配、危险角色属性、角色能切换到其他角色，或在目标库中拥有 schema/关系对象时直接失败，不自动降权、转移对象或覆盖密码。数据库所有权也会检查。
- 先检查所有已存在角色，再创建缺少的角色。角色创建和 CONNECT 授权处于同一事务，失败会回滚；使用事务级 advisory lock 串行化本工具的角色初始化。
- 配置覆盖的对象限于目标库；不声称已审计其他数据库内的对象所有权。角色是集群级对象，应使用项目专用名称。

### 迁移的重复执行约定

- 复用服务自己的 Alembic 代码、head 和角色授权约定，不维护第二份 DDL。
- 启动任何迁移前，先验证全部所选服务的运行角色和真实登录凭据。运行身份不能用于迁移；迁移子进程仅接收自身迁移 URL、角色名与必要的系统环境，不转交初始化 URL、其他服务密码、AWS 凭据或 Claim 密钥。
- 每个服务执行期间持有目标数据库的迁移 advisory lock，防止本工具的并发实例同时迁移；直接运行 Alembic 的其他操作不受此工具锁约束。
- 只执行 `upgrade head`。再次执行会保留数据，并重用服务迁移中已有的授权逻辑。五个服务各自提交，**不是跨服务事务**；某个失败时停止，修复后可从该服务或全部服务重跑。
- 迁移不是权限收敛器：不会自动撤销所有手工添加的授权。额外权限由 `check` 报出，交由授权操作人员审阅后处理。
- 迁移子进程输出不会原样回显；失败以 `migration_failed:服务名` 返回。如需排查详细错误，应在受控操作环境中使用原服务 Alembic 入口，不将原始日志粘贴到公开 Issue。

## 4. 只读权限与迁移检查

`check` 使用真实运行凭据验证登录，再通过 PostgreSQL catalog 和权限函数检查：

- 每个所选服务的 `alembic_version` 精确等于仓库当前 head。
- ORM 声明的表存在，且启用 RLS 和 FORCE RLS；业务表/列没有 PUBLIC 数据权限。
- 运行角色不拥有目标库中的 schema/关系对象，不具危险角色标志或其他角色成员关系，不能创建 schema、不能在业务/public schema 建对象。
- 自身迁移版本可读、必要的业务列可读；迁移版本不可写，业务表无 TRUNCATE、TRIGGER 或 REFERENCES 额外权限。
- 不具其他业务服务表/列的读写权限。Venue API 不可更新场馆状态、identity 不可修改成员，Item internal 不可读取参考答案，Claim worker 不可读取联系人，Notification API 不可读取后台任务；关键 owner 绑定列不可修改。
- Matching 所需 pgvector 扩展存在。

这些是部署前的结构和关键权限边界检查，不是所有 SQL 权限、RLS 谓词或历史数据的形式化审计。原业务集成测试仍负责验证用户/场馆行隔离、合法写入和业务流程。`check` 不修改错误版本、修复 RLS 或补授权；检查失败后先查明原因。

## 5. 密钥检查与轮换边界

`keys` 只读取 `FIELD_ENCRYPTION_KEYS`、`FIELD_ENCRYPTION_ACTIVE_KEY_ID` 和 `HANDOVER_TOKEN_KEY`，不要求数据库配置：

- 字段 keyring 必须符合运行时 `FieldCipher` 约定：1–16 个有效 key ID、严格 base64、每个解码为 32 字节，活动 ID 必须存在；重复 JSON key ID 会被拒绝。
- 交接密钥必须是严格 base64，解码后至少 32 字节；不能复用字段密钥，同一字段 keyring 的不同 ID 也不能指向相同密钥材料。
- 为各个已配置 key ID 产生一次临时随机 nonce 的加密值，使用当前 keyring 解密验证。此自检只在内存中执行，不输出或保存密钥、密文，不修改现有环境。

本命令不生成新密钥、不自动轮换、不扫描历史联系人或 QR 数据。它不能证明已遗漏的历史 key 不再使用；轮换时须保留旧字段 key，确认旧数据完成重加密后才能移除。`HANDOVER_TOKEN_KEY` 的更换也会影响已有交接码，不能为通过检查而临时生成替换值。

## 6. 本地验证

```powershell
$env:LOSTLINK_TEST_ADMIN_URL='postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink'
python -m pytest -q libs/tests/test_backend_admin.py libs/tests/test_backend_admin_integration.py
```

集成测试新建并清理随机命名的测试库与角色，覆盖五服务连续重复初始化/迁移、带特殊字符的密码、独立非超级用户迁移身份、密码不匹配不覆盖、危险角色不接管，以及版本/RLS/跨服务/私密列授权漂移。测试不连接 AWS，也不使用线上秘密。现有共享库与 integration CI 作业会自动收集这些测试。
