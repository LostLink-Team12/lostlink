resource "aws_budgets_budget" "dev" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  # Account-wide deliberately: includes untagged/shared resources and C2 storage.
  dynamic "notification" {
    for_each = { actual = "ACTUAL", forecast = "FORECASTED" }
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = 80
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value
      subscriber_email_addresses = var.budget_notification_emails
    }
  }
}
