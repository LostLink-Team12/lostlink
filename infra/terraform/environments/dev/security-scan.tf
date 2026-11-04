# This optional main-only identity can pull deployment digests, never deploy,
# mutate ECR, read Secrets Manager or access the Kubernetes API.
resource "aws_iam_role" "github_security_scan" {
  count = var.c3_enabled ? 1 : 0
  name  = "${local.name}-github-security-scan"
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

resource "aws_iam_role_policy" "github_security_scan" {
  count = var.c3_enabled ? 1 : 0
  role  = aws_iam_role.github_security_scan[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability", "ecr:DescribeImages"]
        Resource = concat(
          [for service in ["venue", "item", "matching", "claim", "notification"] : aws_ecr_repository.services["${service}-service"].arn],
          [aws_ecr_repository.frontend[0].arn]
        )
      }
    ]
  })
}

output "github_security_scan_role_arn" {
  value = try(aws_iam_role.github_security_scan[0].arn, null)
}
