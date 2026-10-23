# 首次自动部署失败的修复与交接

## 已确认的故障

PR #25 合并后的 main 提交 `6e342ada86542cc032a391feb09f69a645152a17` 触发了
[CI #39954371609](https://github.com/LostLink-Team12/lostlink/actions/runs/39954371609)。
测试、扫描和 8 项镜像发布成功；`Deploy dev EKS` 在申请部署身份时失败，随后清理步骤也报错。
访问租约、kubeconfig 和发布步骤未执行，因此本次没有更新应用，也没有新增 runner `/32`。
镜像已经进入 ECR 不代表应用已经部署。

### 部署角色的 OIDC 信任主体过时

仓库 API `GET /repos/LostLink-Team12/lostlink/actions/oidc/customization/sub` 返回
`use_immutable_subject=true`，前缀为：

```text
repo:LostLink-Team12@343981760/lostlink@1423380955
```

原 `cd.tf` 使用旧主体 `repo:LostLink-Team12/lostlink:ref:refs/heads/main`。
现改为精确匹配：

```text
repo:LostLink-Team12@343981760/lostlink@1423380955:ref:refs/heads/main
```

仍仅允许 `sts.amazonaws.com` audience、当前账号的 GitHub OIDC provider、当前仓库 main。
没有开放其他分支、PR 或通配符，也不应把仓库 OIDC 改回旧格式来绕过问题。
新增 Terraform mock 断言检查整个条件对象、主体、Action、Effect 和单条 Statement。

本机没有 AWS 操作身份，没有读取或更新线上 IAM。日志中的拒绝与仓库信任配置不匹配一致；
线上策略的最终内容仍需有权限的组员核实。参考
[GitHub 官方 OIDC 文档](https://docs.github.com/en/actions/reference/security/oidc)。

### boto3 client 生命周期错误

锁定版本的 boto3 client 支持 `.close()`，不支持 `with client`。
`cd_access.py` 的 EKS client，以及 `cd_deploy.py` 的 STS、EKS、ECR client
均改用 `contextlib.closing`，正常结束和抛异常时都会关闭。
没有 lease 文件时，`close` 在创建 AWS client 前直接退出；这表示未创建过访问租约，
不需要 AWS 凭据，也不会掩盖前面的身份认证失败。有 lease 时继续执行原有精确清理，失败仍报告失败。

新增 CLI 入口测试使用显式假凭据、真实 SDK client、Stubber 和网络阻断；验证申请/清理成功与异常、
错误账号/集群的拒绝、镜像查询失败、发布失败及 client 关闭。原有租约并发保留、回滚和权限约束继续有效。

## 组员需要执行：更新线上部署角色信任策略

需要有权管理 `152174417545` 账号中 `lostlink-dev-github-deploy` 信任策略的组员执行。
不需要把个人 AWS key 发给 Agent，也不需要给现有 CI 发布角色增加部署权限。

1. 在保留原有 dev backend、变量文件的操作员 checkout 获取本修复的代码；建议 PR 合并后更新 main。
   保留 `infra/terraform/environments/dev/terraform.tfvars`、`dev.s3.tfbackend` 及
   `infra/deploy/.local/infrastructure.tfvars.json`，不要重新运行 `configure.py infrastructure`
   覆盖现有值（其中包含已使用的随机 origin header 等配置）。
2. 确认没有 CD 或 Terraform 操作正在修改同一环境。使用组员已有 profile，先核实账号：

```powershell
$env:AWS_PROFILE = "lostlink-dev" # 替换为组员已有的有效 profile 名称
$deploymentAccount = aws sts get-caller-identity --query Account --output text
if ($LASTEXITCODE -ne 0 -or $deploymentAccount.Trim() -ne "152174417545") {
    throw "AWS account verification failed"
}
aws iam get-role --role-name lostlink-dev-github-deploy --query Role.AssumeRolePolicyDocument --output json
```

3. 使用原有 remote state 和变量执行常规 Terraform 计划，不创建新 backend，不使用 `-target`：

```powershell
terraform -chdir=infra/terraform/environments/dev init -backend-config=dev.s3.tfbackend
terraform -chdir=infra/terraform/environments/dev plan -var-file=../../../deploy/.local/infrastructure.tfvars.json -out=cd-trust-fix.tfplan
terraform -chdir=infra/terraform/environments/dev show -no-color cd-trust-fix.tfplan
```

预期仅 `aws_iam_role.github_deploy[0].assume_role_policy` 原地变更，将旧 `sub` 替换为上述精确值。
若计划包含节点、网络、数据库、origin header、其他资源变更或销毁，应先核对原有变量/state，
不要把它当成此次修复直接 apply。若线上策略已经正确，可能为零变更，此时进一步核查
Actions 的 `AWS_DEPLOY_ROLE_ARN` 是否确实指向这个角色。

4. 核对计划后应用已保存的计划，再只读确认角色信任策略：

```powershell
terraform -chdir=infra/terraform/environments/dev apply cd-trust-fix.tfplan
aws iam get-role --role-name lostlink-dev-github-deploy --query Role.AssumeRolePolicyDocument --output json
```

最终应只有当前账号的 `token.actions.githubusercontent.com` provider，Action 为
`sts:AssumeRoleWithWebIdentity`，`StringEquals` 的 audience 为 `sts.amazonaws.com`，
`sub` 为本文件列出的 immutable main 主体。现有权限策略、EKS access entry、RBAC 无需扩大。

## 修复合并后的重新验证

必须同时满足“本修复已经合并 main”和“线上信任策略已更新”。仅合并 Terraform 文件不会自动 apply AWS。
PR CI 不请求部署身份，`publish-image` 和 `Deploy dev EKS` 在 PR 上跳过是预期行为，无法证明云端已修复。

合并 main 会自动启动 CI。建议组员先更新 IAM，再合并修复。若新 main 的镜像已经发布、
只是部署因 IAM 未更新而失败，等组员完成后，打开这个**包含修复的新 main run**，选择
`Re-run failed jobs`，只重试失败的部署，保留成功的镜像发布结果。

若尚无这个 main 的 CI run，可以在 GitHub Actions 中选 `CI` →
`Run workflow` → branch 选择 `main` → `Run workflow`。命令行等价操作：

```powershell
gh workflow run ci.yml --repo LostLink-Team12/lostlink --ref main
gh run list --repo LostLink-Team12/lostlink --workflow ci.yml --branch main --limit 5
# 使用上一步新产生的 run ID；不要照抄旧失败 run 的 ID。
gh run watch <new-run-id> --repo LostLink-Team12/lostlink --exit-status
```

不要重跑旧提交 `6e342ada...` 的失败部署来验证 Python 修复；旧 run 使用旧脚本，且较旧 main 会被跳过。
手动新建整个 CI run 会重新构建和推送；ECR 标签不可变，重复使用已有 SHA 标签可能被拒绝，
因此部署失败后优先重跑新 main run 的失败 job，而不是重跑所有成功的发布 job。
当前流程从选定 main 提交构建、扫描、发布并部署对应 digest，不提供任意镜像选择。

验收应看到申请部署身份、runner `/32` 租约、kubeconfig、20 项 rollout、HTTPS smoke、租约清理均成功。
保留 run 链接及部署源 SHA；若失败，按具体失败步骤继续诊断，不能仅凭 ECR 发布成功确认部署完成。
本修复不更改数据库迁移审批基线；若新 main 同时改变迁移，仍需按原操作员流程完成迁移及基线审批。

## 本次验证记录

- 本地部署测试：34 passed（原 24 项及新增 10 项 CLI 回归），不连接 AWS。
- Ruff 检查及格式检查通过。
- Terraform 1.16.6：格式、validate 和 17 项 dev mock 测试全部通过；backend 禁用。
- PR CI 结果见本次 PR 的检查记录。
- 未执行 AWS apply、未触发真实部署；线上身份及完整发布验收待组员完成上述操作。
