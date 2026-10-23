variable "token_trigger_package" {
  description = "Absolute path to the Linux Python 3.12 zip built by build-token-trigger.ps1; null until DB bootstrap completes."
  type        = string
  default     = null
}

variable "token_trigger_sha256" {
  description = "Base64 SHA256 of the zip; passed explicitly so mock plans need no build artifact."
  type        = string
  default     = null
}

variable "token_trigger_pool_id" {
  description = "Existing pool ID; avoids a pool -> Lambda environment -> pool Terraform cycle."
  type        = string
  default     = null
}

variable "token_trigger_web_client_id" {
  type    = string
  default = null
}

variable "token_trigger_operator_client_id" {
  type    = string
  default = null
}

variable "attach_token_trigger" {
  description = "Attach only after migrations and a successful direct Lambda invocation."
  type        = bool
  default     = false
  validation {
    condition = !var.attach_token_trigger || (var.application_runtime_enabled &&
      var.token_trigger_package != null && var.token_trigger_sha256 != null &&
      var.token_trigger_pool_id != null && var.token_trigger_web_client_id != null &&
    var.token_trigger_operator_client_id != null)
    error_message = "Attaching the trigger requires its built package/hash and existing pool, owner, and operator client IDs."
  }
}

locals {
  token_trigger_enabled = var.application_runtime_enabled && var.token_trigger_package != null
}

check "trigger_configuration" {
  assert {
    condition     = !local.token_trigger_enabled || (var.token_trigger_sha256 != null && var.token_trigger_pool_id != null && var.token_trigger_web_client_id != null)
    error_message = "A trigger package requires its hash and the already-created Cognito pool/web client IDs."
  }
  assert {
    condition     = !var.attach_token_trigger || local.token_trigger_enabled
    error_message = "Build and test the token trigger before attaching it."
  }
  assert {
    condition = !local.token_trigger_enabled || (var.token_trigger_pool_id == aws_cognito_user_pool.this.id &&
      var.token_trigger_web_client_id == aws_cognito_user_pool_client.frontend.id &&
    (var.token_trigger_operator_client_id == null || var.token_trigger_operator_client_id == try(aws_cognito_user_pool_client.operator[0].id, null)))
    error_message = "Trigger IDs must match this environment's actual Cognito pool and browser client."
  }
}

resource "aws_security_group" "token_trigger" {
  count       = local.token_trigger_enabled ? 1 : 0
  name_prefix = "${local.name}-token-trigger-"
  description = "Token trigger can reach only its database and Secrets Manager endpoint"
  vpc_id      = aws_vpc.this.id
}

resource "aws_security_group" "secrets_endpoint" {
  count       = local.token_trigger_enabled ? 1 : 0
  name_prefix = "${local.name}-secrets-endpoint-"
  description = "TLS from the token trigger and application nodes"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_egress_rule" "trigger_database" {
  count                        = local.token_trigger_enabled ? 1 : 0
  security_group_id            = aws_security_group.token_trigger[0].id
  referenced_security_group_id = aws_security_group.database.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "database_trigger" {
  count                        = local.token_trigger_enabled ? 1 : 0
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_security_group.token_trigger[0].id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "trigger_secrets" {
  count                        = local.token_trigger_enabled ? 1 : 0
  security_group_id            = aws_security_group.token_trigger[0].id
  referenced_security_group_id = aws_security_group.secrets_endpoint[0].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "secrets_trigger" {
  count                        = local.token_trigger_enabled ? 1 : 0
  security_group_id            = aws_security_group.secrets_endpoint[0].id
  referenced_security_group_id = aws_security_group.token_trigger[0].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "secrets_nodes" {
  count                        = local.token_trigger_enabled ? 1 : 0
  security_group_id            = aws_security_group.secrets_endpoint[0].id
  referenced_security_group_id = aws_security_group.nodes.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "secrets" {
  count               = local.token_trigger_enabled ? 1 : 0
  vpc_id              = aws_vpc.this.id
  vpc_endpoint_type   = "Interface"
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  private_dns_enabled = true
  # Single-AZ endpoint is deliberate for the short student demo, not HA.
  subnet_ids         = [aws_subnet.private[0].id]
  security_group_ids = [aws_security_group.secrets_endpoint[0].id]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Principal = "*", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource  = concat([for secret in aws_secretsmanager_secret.application : secret.arn], [for secret in aws_secretsmanager_secret.machine : secret.arn])
      Condition = { StringEquals = { "aws:PrincipalAccount" = var.aws_account_id } }
    }]
  })
}

resource "aws_iam_role" "token_trigger" {
  count = local.token_trigger_enabled ? 1 : 0
  name  = "${local.name}-token-trigger"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_cloudwatch_log_group" "token_trigger" {
  count             = local.token_trigger_enabled ? 1 : 0
  name              = "/aws/lambda/${local.name}-token-trigger"
  retention_in_days = 7
  kms_key_id        = aws_kms_key.dev.arn
}

resource "aws_iam_role_policy" "token_trigger" {
  count = local.token_trigger_enabled ? 1 : 0
  role  = aws_iam_role.token_trigger[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.token_trigger[0].arn}:*" },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.application["database/venue/identity"].arn },
      {
        Effect    = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.dev.arn
        Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.aws_region}.amazonaws.com", "kms:EncryptionContext:SecretARN" = aws_secretsmanager_secret.application["database/venue/identity"].arn } }
      },
      # AWS requires Resource * for these Lambda Hyperplane ENI operations.
      { Effect = "Allow", Action = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"], Resource = "*" },
      # Function code itself must not use its service's ENI privileges.
      {
        Effect    = "Deny", Action = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"], Resource = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:${local.name}-token-trigger" } }
      }
    ]
  })
}

resource "aws_lambda_function" "token_trigger" {
  count            = local.token_trigger_enabled ? 1 : 0
  function_name    = "${local.name}-token-trigger"
  role             = aws_iam_role.token_trigger[0].arn
  filename         = var.token_trigger_package
  source_code_hash = var.token_trigger_sha256
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  handler          = "lambda_entry.handler"
  # More CPU shortens cold imports/TLS below Cognito's fixed 5-second deadline.
  # On-demand execution only; there is no always-on provisioned concurrency.
  memory_size = 2048
  timeout     = 5
  # The student account must retain all 10 unreserved executions. A reserved
  # limit is rejected by AWS; each execution still uses a one-connection pool.
  environment {
    variables = {
      IDENTITY_SECRET_ARN     = aws_secretsmanager_secret.application["database/venue/identity"].arn
      COGNITO_USER_POOL_ID    = var.token_trigger_pool_id
      USER_CLIENT_IDS         = jsonencode([var.token_trigger_web_client_id])
      PLATFORM_CLIENT_IDS     = jsonencode([var.token_trigger_operator_client_id == null ? var.token_trigger_web_client_id : var.token_trigger_operator_client_id])
      DB_POOL_SIZE            = "1"
      DB_MAX_OVERFLOW         = "0"
      DB_STATEMENT_TIMEOUT_MS = "1500"
      ENVIRONMENT             = "dev"
    }
  }
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.token_trigger[0].id]
  }
  depends_on = [aws_iam_role_policy.token_trigger, aws_vpc_endpoint.secrets]
}

resource "aws_lambda_permission" "cognito" {
  count          = local.token_trigger_enabled ? 1 : 0
  statement_id   = "CognitoPreTokenGeneration"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.token_trigger[0].function_name
  principal      = "cognito-idp.amazonaws.com"
  source_arn     = aws_cognito_user_pool.this.arn
  source_account = var.aws_account_id
}

output "token_trigger_function_name" {
  value = local.token_trigger_enabled ? aws_lambda_function.token_trigger[0].function_name : null
}
