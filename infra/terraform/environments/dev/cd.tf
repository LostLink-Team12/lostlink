# Separate from C2 image publishing. Main-only identity, named cluster only;
# Kubernetes mutations are restricted by infra/deploy/cd-rbac.yaml.
# This repository uses immutable GitHub owner/repository IDs in its OIDC subject.
resource "aws_iam_role" "github_deploy" {
  count                = var.c3_enabled ? 1 : 0
  name                 = "${local.name}-github-deploy"
  max_session_duration = 7200
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = "arn:aws:iam::${var.aws_account_id}:oidc-provider/token.actions.githubusercontent.com"
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:LostLink-Team12@343981760/lostlink@1423380955:ref:refs/heads/main"
        }
      }
    }]
  })
}

# IAM Policy Autopilot baseline, narrowed to actual resources. Omit its optional
# CreateAccessEntry/PassRole expansion: CD changes CIDRs, never compute or IAM.
resource "aws_iam_role_policy" "github_deploy" {
  count = var.c3_enabled ? 1 : 0
  role  = aws_iam_role.github_deploy[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["eks:DescribeCluster", "eks:DescribeUpdate", "eks:UpdateClusterConfig"]
        Resource = aws_eks_cluster.this.arn
      },
      {
        Effect = "Allow"
        Action = ["ecr:DescribeImages"]
        Resource = concat(
          [for service in ["venue", "item", "matching", "claim", "notification"] : aws_ecr_repository.services["${service}-service"].arn],
          [aws_ecr_repository.frontend[0].arn]
        )
      }
    ]
  })
}

resource "aws_eks_access_entry" "github_deploy" {
  count             = var.c3_enabled ? 1 : 0
  cluster_name      = aws_eks_cluster.this.name
  principal_arn     = aws_iam_role.github_deploy[0].arn
  kubernetes_groups = ["lostlink-cd"]
  type              = "STANDARD"
}

output "github_deploy_role_arn" {
  value = try(aws_iam_role.github_deploy[0].arn, null)
}
