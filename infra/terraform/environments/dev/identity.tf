resource "aws_cognito_user_pool" "this" {
  name                     = local.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "ACTIVE"
  mfa_configuration        = "OPTIONAL"
  user_pool_tier           = "ESSENTIALS"
  dynamic "lambda_config" {
    for_each = var.attach_token_trigger ? [1] : []
    content {
      pre_token_generation_config {
        lambda_arn     = aws_lambda_function.token_trigger[0].arn
        lambda_version = "V2_0"
      }
    }
  }
  software_token_mfa_configuration {
    enabled = true
  }
  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_client" "frontend" {
  name                                 = "${local.name}-web"
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = false
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  allowed_oauth_flows_user_pool_client = true
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

resource "aws_cognito_user_pool_domain" "this" {
  domain       = "${local.name}-${var.aws_account_id}"
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_group" "staff" {
  name         = "venue-staff"
  user_pool_id = aws_cognito_user_pool.this.id
  description  = "Venue staff; per-venue tenant authorization remains in the application"
}

resource "aws_ses_email_identity" "notifications" {
  count = var.notification_sender_email == null ? 0 : 1
  email = var.notification_sender_email
}

locals {
  # Only services that call AWS receive roles. Database grants are separate SQL grants.
  workload_statements = {
    "venue-service" = [{
      Effect   = "Allow"
      Action   = ["cognito-idp:AdminCreateUser", "cognito-idp:AdminGetUser", "cognito-idp:AdminAddUserToGroup", "cognito-idp:AdminRemoveUserFromGroup"]
      Resource = aws_cognito_user_pool.this.arn
    }]
    "item-service" = [
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "${aws_s3_bucket.media.arn}/items/*" },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.jobs["embedding"].arn },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.events["item.registered"].arn },
      {
        Effect    = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.dev.arn
        Condition = { StringEquals = { "kms:ViaService" = ["s3.${var.aws_region}.amazonaws.com", "sns.${var.aws_region}.amazonaws.com"] } }
      }
    ]
    "embedding-worker" = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.media.arn}/items/*" },
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.jobs["embedding"].arn },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.events["item.embedded"].arn },
      {
        Effect    = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.dev.arn
        Condition = { StringEquals = { "kms:ViaService" = ["s3.${var.aws_region}.amazonaws.com", "sns.${var.aws_region}.amazonaws.com"] } }
      }
    ]
    "matching-service" = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.media.arn}/items/*" },
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.jobs["matching"].arn },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.events["match.found"].arn },
      {
        Effect    = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.dev.arn
        Condition = { StringEquals = { "kms:ViaService" = ["s3.${var.aws_region}.amazonaws.com", "sns.${var.aws_region}.amazonaws.com"] } }
      }
    ]
    "notification-service" = concat([
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.jobs["notifications"].arn }
      ], var.notification_sender_email == null ? [] : [{
        Effect = "Allow", Action = ["ses:SendEmail", "ses:SendRawEmail"], Resource = aws_ses_email_identity.notifications[0].arn
    }])
    "claim-service" = [
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.events["claim.approved"].arn },
      {
        Effect    = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.dev.arn
        Condition = { StringEquals = { "kms:ViaService" = "sns.${var.aws_region}.amazonaws.com" } }
      }
    ]
  }
}

resource "aws_iam_role" "workload" {
  for_each = local.workload_statements
  name     = "${local.name}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = "system:serviceaccount:lostlink:${each.key}"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "workload" {
  for_each = local.workload_statements
  role     = aws_iam_role.workload[each.key].id
  policy   = jsonencode({ Version = "2012-10-17", Statement = each.value })
}
