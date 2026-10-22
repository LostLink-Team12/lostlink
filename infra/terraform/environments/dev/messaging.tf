locals {
  event_topics = toset(["item.registered", "item.embedded", "match.found", "claim.approved"])
  queues       = toset(["embedding", "matching", "notifications"])
  subscriptions = {
    matching      = "item.embedded"
    notifications = "match.found"
  }
}

resource "aws_sns_topic" "events" {
  for_each          = local.event_topics
  name              = "${local.name}-${replace(each.key, ".", "-")}"
  kms_master_key_id = aws_kms_key.dev.id
}

resource "aws_sqs_queue" "dead_letter" {
  for_each                  = local.queues
  name                      = "${local.name}-${each.key}-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "jobs" {
  for_each                   = local.queues
  name                       = "${local.name}-${each.key}"
  message_retention_seconds  = 86400
  visibility_timeout_seconds = each.key == "embedding" ? 600 : 120
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead_letter[each.key].arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dead_letter" {
  for_each  = local.queues
  queue_url = aws_sqs_queue.dead_letter[each.key].url
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue", sourceQueueArns = [aws_sqs_queue.jobs[each.key].arn]
  })
}

resource "aws_sqs_queue_policy" "jobs" {
  for_each  = local.queues
  queue_url = aws_sqs_queue.jobs[each.key].url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid       = "RequireTLS", Effect = "Deny", Principal = "*", Action = "sqs:*"
        Resource  = aws_sqs_queue.jobs[each.key].arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }
      ], contains(keys(local.subscriptions), each.key) ? [{
        Sid    = "AllowSubscribedTopic", Effect = "Allow", Principal = { Service = "sns.amazonaws.com" }
        Action = "sqs:SendMessage", Resource = aws_sqs_queue.jobs[each.key].arn
        Condition = {
          ArnEquals    = { "aws:SourceArn" = aws_sns_topic.events[local.subscriptions[each.key]].arn }
          StringEquals = { "aws:SourceAccount" = var.aws_account_id }
        }
    }] : [])
  })
}

resource "aws_sqs_queue_policy" "dead_letter" {
  for_each  = local.queues
  queue_url = aws_sqs_queue.dead_letter[each.key].url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "RequireTLS", Effect = "Deny", Principal = "*", Action = "sqs:*"
      Resource  = aws_sqs_queue.dead_letter[each.key].arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_sns_topic_subscription" "internal" {
  for_each             = local.subscriptions
  topic_arn            = aws_sns_topic.events[each.value].arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.jobs[each.key].arn
  raw_message_delivery = true
  depends_on           = [aws_sqs_queue_policy.jobs]
}
