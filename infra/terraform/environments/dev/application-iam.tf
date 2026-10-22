locals {
  # Each entry is also the Kubernetes service-account name. Split the Item
  # sealer from the API so a presigned upload can never write sealed/ directly.
  process_contracts = var.application_runtime_enabled ? {
    venue-api            = { db = ["venue/api", "venue/platform"], machine = [], crypto = false, publish = false, queue = null, storage = null, directory = [] }
    venue-outbox         = { db = ["venue/worker"], machine = [], crypto = false, publish = true, queue = null, storage = null, directory = [] }
    venue-identity       = { db = ["venue/worker"], machine = [], crypto = false, publish = false, queue = null, storage = null, directory = ["cognito-idp:ListUsers", "cognito-idp:AdminUserGlobalSignOut"] }
    item-api             = { db = ["item/api", "item/internal"], machine = ["item"], crypto = false, publish = false, queue = null, storage = "upload", directory = [] }
    item-tasks           = { db = ["item/worker"], machine = [], crypto = false, publish = false, queue = null, storage = "seal", directory = [] }
    item-outbox          = { db = ["item/worker"], machine = [], crypto = false, publish = true, queue = null, storage = null, directory = [] }
    item-consume         = { db = ["item/worker"], machine = [], crypto = false, publish = false, queue = "item", storage = null, directory = [] }
    item-cleanup         = { db = ["item/worker"], machine = [], crypto = false, publish = false, queue = null, storage = "cleanup", directory = [] }
    matching-api         = { db = ["matching/api", "matching/internal"], machine = ["matching"], crypto = false, publish = false, queue = null, storage = null, directory = [] }
    matching-tasks       = { db = ["matching/worker"], machine = ["matching"], crypto = false, publish = false, queue = null, storage = "embed", directory = [] }
    matching-consume     = { db = ["matching/worker"], machine = [], crypto = false, publish = false, queue = "matching", storage = null, directory = [] }
    matching-outbox      = { db = ["matching/worker"], machine = [], crypto = false, publish = true, queue = null, storage = null, directory = [] }
    claim-api            = { db = ["claim/api"], machine = ["claim"], crypto = true, publish = false, queue = null, storage = null, directory = ["cognito-idp:ListUsers"] }
    claim-tasks          = { db = ["claim/worker"], machine = ["claim"], crypto = true, publish = false, queue = null, storage = null, directory = ["cognito-idp:ListUsers"] }
    claim-outbox         = { db = ["claim/worker"], machine = [], crypto = false, publish = true, queue = null, storage = null, directory = [] }
    notification-api     = { db = ["notification/api", "notification/admin"], machine = [], crypto = false, publish = false, queue = null, storage = null, directory = [] }
    notification-consume = { db = ["notification/worker"], machine = [], crypto = false, publish = false, queue = "notifications", storage = null, directory = [] }
    notification-tasks   = { db = ["notification/worker"], machine = ["notification"], crypto = false, publish = false, queue = null, storage = null, directory = ["cognito-idp:ListUsers"] }
  } : {}
  storage_statements = {
    upload = [
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.media.arn}/staging/*" },
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.media.arn}/staging/*", "${aws_s3_bucket.media.arn}/sealed/*"] },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.media.arn }
    ]
    seal = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.media.arn}/staging/*", "${aws_s3_bucket.media.arn}/sealed/*"] },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.media.arn}/sealed/*" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.media.arn }
    ]
    cleanup = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.media.arn, Condition = { StringLike = { "s3:prefix" = ["staging/*"] } } },
      { Effect = "Allow", Action = ["s3:DeleteObject"], Resource = "${aws_s3_bucket.media.arn}/staging/*" }
    ]
    embed = [{ Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.media.arn}/sealed/*" }]
  }
  process_secrets = { for key, process in local.process_contracts : key => concat(
    [for db in process.db : aws_secretsmanager_secret.application["database/${db}"].arn],
    [for machine in process.machine : aws_secretsmanager_secret.machine[machine].arn],
    process.crypto ? [aws_secretsmanager_secret.application["claim/crypto"].arn] : []
  ) }
  process_statements = { for key, process in local.process_contracts : key => concat(
    [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = local.process_secrets[key] }],
    [{
      Effect    = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.dev.arn
      Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.aws_region}.amazonaws.com" }, ArnEquals = { "kms:EncryptionContext:SecretARN" = local.process_secrets[key] } }
    }],
    jsondecode(process.publish ? jsonencode([
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.application[0].arn },
      { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.dev.arn, Condition = { StringEquals = { "kms:ViaService" = "sns.${var.aws_region}.amazonaws.com" } } }
    ]) : "[]"),
    process.queue == null ? [] : [{ Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.application[process.queue].arn }],
    jsondecode(process.storage == null ? "[]" : jsonencode(concat(local.storage_statements[process.storage], [{
      Effect    = "Allow", Action = process.storage == "embed" ? ["kms:Decrypt"] : ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.dev.arn
      Condition = { StringEquals = { "kms:ViaService" = "s3.${var.aws_region}.amazonaws.com" } }
    }]))),
    length(process.directory) == 0 ? [] : [{ Effect = "Allow", Action = process.directory, Resource = aws_cognito_user_pool.this.arn }],
    key != "notification-tasks" || var.notification_sender_email == null ? [] : [{ Effect = "Allow", Action = ["ses:SendEmail"], Resource = aws_ses_email_identity.notifications[0].arn }]
  ) }
}

resource "aws_iam_role" "application" {
  for_each = local.process_contracts
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

resource "aws_iam_role_policy" "application" {
  for_each = local.process_contracts
  role     = aws_iam_role.application[each.key].id
  policy   = jsonencode({ Version = "2012-10-17", Statement = local.process_statements[each.key] })
}
