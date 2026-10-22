resource "aws_kms_key" "dev" {
  description             = "LostLink dev data, events and logs"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DelegateToAccountIAM", Effect = "Allow"
        Principal = { AWS = "${local.account_arn}:root" }, Action = "kms:*", Resource = "*"
      },
      {
        Sid       = "EncryptCloudWatchLogs", Effect = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource  = "*"
        Condition = { ArnLike = { "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:*" } }
      }
    ]
  })
}

resource "aws_kms_alias" "dev" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.dev.key_id
}
