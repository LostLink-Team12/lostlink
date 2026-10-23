# Phase 1 contracts in docs/team-alignment.md. The original C1 resources stay
# intact so this upgrade does not discard queued messages or replace the pool.
variable "application_runtime_enabled" {
  type    = bool
  default = false
}

variable "notification_test_recipient_email" {
  type     = string
  default  = null
  nullable = true
  validation {
    condition     = var.notification_test_recipient_email == null || can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", var.notification_test_recipient_email))
    error_message = "Use a real SES sandbox test recipient email."
  }
}

locals {
  db_modes = {
    venue        = ["api", "platform", "worker", "identity", "migration"]
    item         = ["api", "internal", "worker", "migration"]
    matching     = ["api", "internal", "worker", "migration"]
    claim        = ["api", "worker", "migration"]
    notification = ["api", "admin", "worker", "migration"]
  }
  db_roles = merge([for service, modes in local.db_modes : {
    for mode in modes : "${service}/${mode}" => "lostlink_${service}_${mode}"
  }]...)
  app_secret_names = var.application_runtime_enabled ? toset(concat(
    [for key in keys(local.db_roles) : "database/${key}"], ["claim/crypto"]
  )) : toset([])
  machine_scopes = var.application_runtime_enabled ? {
    item         = ["members.check"]
    claim        = ["members.check", "items.context", "items.reserve"]
    matching     = ["items.index"]
    notification = ["matches.notify"]
  } : {}
  app_routes = var.application_runtime_enabled ? {
    matching      = ["item.registered", "item.catalog_changed"]
    item          = ["item.embedded", "item.embedding_failed"]
    notifications = ["match.found"]
  } : {}
}

# Values are initialized by bootstrap-app.py, never by Terraform/random_password.
# Runtime passwords therefore do not enter Terraform state or plan artifacts.
resource "aws_secretsmanager_secret" "application" {
  for_each                = local.app_secret_names
  name                    = "${local.name}/${each.key}"
  kms_key_id              = aws_kms_key.dev.arn
  recovery_window_in_days = 7
}

resource "aws_cognito_resource_server" "application" {
  count        = var.application_runtime_enabled ? 1 : 0
  identifier   = "lostlink"
  name         = "LostLink internal APIs"
  user_pool_id = aws_cognito_user_pool.this.id
  dynamic "scope" {
    for_each = toset(flatten(values(local.machine_scopes)))
    content {
      scope_name        = scope.value
      scope_description = "LostLink ${scope.value}"
    }
  }
}

# Notification requires disjoint owner/operator clients; frontend already
# selects separate ownerClientId and venueClientId settings.
resource "aws_cognito_user_pool_client" "operator" {
  count                                = var.application_runtime_enabled ? 1 : 0
  name                                 = "${local.name}-operator-web"
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = local.callback_urls
  logout_urls                          = local.logout_urls
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 60
  id_token_validity                    = 60
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_pool_client" "machine" {
  for_each                             = local.machine_scopes
  name                                 = "${local.name}-${each.key}-machine"
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = [for scope in each.value : "lostlink/${scope}"]
  explicit_auth_flows                  = []
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 60
  id_token_validity                    = 60
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
  depends_on = [aws_cognito_resource_server.application]
}

resource "aws_secretsmanager_secret" "machine" {
  for_each                = local.machine_scopes
  name                    = "${local.name}/machine/${each.key}"
  kms_key_id              = aws_kms_key.dev.arn
  recovery_window_in_days = 7
}

# Cognito-generated client secrets are necessarily present in encrypted remote
# Terraform state. Do not publish plans/state; runtime DB/crypto secrets are not.
resource "aws_secretsmanager_secret_version" "machine" {
  for_each  = local.machine_scopes
  secret_id = aws_secretsmanager_secret.machine[each.key].id
  secret_string = jsonencode({
    client_id     = aws_cognito_user_pool_client.machine[each.key].id
    client_secret = aws_cognito_user_pool_client.machine[each.key].client_secret
    token_url     = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/token"
    scopes        = [for scope in each.value : "lostlink/${scope}"]
  })
}

resource "aws_ses_email_identity" "test_recipient" {
  count = var.notification_test_recipient_email == null ? 0 : 1
  email = var.notification_test_recipient_email
}

resource "aws_sns_topic" "application" {
  count             = var.application_runtime_enabled ? 1 : 0
  name              = "${local.name}-domain-events"
  kms_master_key_id = aws_kms_key.dev.id
}

resource "aws_sqs_queue" "application_dlq" {
  for_each                  = local.app_routes
  name                      = "${local.name}-${each.key}-events-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "application" {
  for_each                   = local.app_routes
  name                       = "${local.name}-${each.key}-events"
  message_retention_seconds  = 86400
  visibility_timeout_seconds = 120
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.application_dlq[each.key].arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "application" {
  for_each  = local.app_routes
  queue_url = aws_sqs_queue.application_dlq[each.key].url
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue", sourceQueueArns = [aws_sqs_queue.application[each.key].arn]
  })
}

resource "aws_sqs_queue_policy" "application" {
  for_each  = local.app_routes
  queue_url = aws_sqs_queue.application[each.key].url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS", Effect = "Deny", Principal = "*", Action = "sqs:*"
        Resource  = aws_sqs_queue.application[each.key].arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid    = "AllowDomainEvents", Effect = "Allow", Principal = { Service = "sns.amazonaws.com" }
        Action = "sqs:SendMessage", Resource = aws_sqs_queue.application[each.key].arn
        Condition = {
          ArnEquals    = { "aws:SourceArn" = aws_sns_topic.application[0].arn }
          StringEquals = { "aws:SourceAccount" = var.aws_account_id }
        }
      }
    ]
  })
}

resource "aws_sqs_queue_policy" "application_dlq" {
  for_each  = local.app_routes
  queue_url = aws_sqs_queue.application_dlq[each.key].url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.application_dlq[each.key].arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_sns_topic_subscription" "application" {
  for_each             = local.app_routes
  topic_arn            = aws_sns_topic.application[0].arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.application[each.key].arn
  raw_message_delivery = true
  filter_policy_scope  = "MessageAttributes"
  filter_policy        = jsonencode({ event_type = each.value })
  depends_on           = [aws_sqs_queue_policy.application]
}

output "application" {
  description = "Non-secret deployment contract; credential values stay in Secrets Manager."
  value = var.application_runtime_enabled ? {
    database_roles       = local.db_roles
    database_secret_arns = { for key, secret in aws_secretsmanager_secret.application : key => secret.arn }
    machine_secret_arns  = { for key, secret in aws_secretsmanager_secret.machine : key => secret.arn }
    machine_client_ids   = { for key, client in aws_cognito_user_pool_client.machine : key => client.id }
    event_topic_arn      = aws_sns_topic.application[0].arn
    queue_urls           = { for key, queue in aws_sqs_queue.application : key => queue.url }
    runtime_role_arns    = { for key, role in aws_iam_role.application : key => role.arn }
    user_pool_id         = aws_cognito_user_pool.this.id
    web_client_id        = aws_cognito_user_pool_client.frontend.id
    operator_client_id   = aws_cognito_user_pool_client.operator[0].id
    issuer               = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.this.id}"
    kms_key_arn          = aws_kms_key.dev.arn
  } : null
}
