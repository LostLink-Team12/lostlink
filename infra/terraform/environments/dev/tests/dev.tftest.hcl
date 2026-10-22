mock_provider "aws" {}

# The tracked adoption block must also be testable without importing from AWS.
override_resource {
  target          = aws_ecr_repository.services["service-template"]
  override_during = plan
  values          = { id = "lostlink/service-template" }
}

override_resource {
  target          = aws_s3_bucket.media
  override_during = plan
  values          = { arn = "arn:aws:s3:::test-media" }
}

override_resource {
  target          = aws_cognito_user_pool.this
  override_during = plan
  values          = { id = "ap-southeast-1_test", arn = "arn:aws:cognito-idp:ap-southeast-1:123456789012:userpool/ap-southeast-1_test" }
}

override_resource {
  target          = aws_cognito_user_pool_client.frontend
  override_during = plan
  values          = { id = "testwebclient" }
}

# Distinct IDs at plan time let assertions verify public/private node routing.
override_resource {
  target          = aws_subnet.public[0]
  override_during = plan
  values          = { id = "subnet-00000000000000001" }
}

override_resource {
  target          = aws_subnet.public[1]
  override_during = plan
  values          = { id = "subnet-00000000000000002" }
}

override_resource {
  target          = aws_subnet.private[0]
  override_during = plan
  values          = { id = "subnet-00000000000000003" }
}

override_resource {
  target          = aws_subnet.private[1]
  override_during = plan
  values          = { id = "subnet-00000000000000004" }
}

override_resource {
  target          = aws_route_table.public
  override_during = plan
  values          = { id = "rtb-00000000000000001" }
}

override_resource {
  target          = aws_route_table.private[0]
  override_during = plan
  values          = { id = "rtb-00000000000000002" }
}

override_resource {
  target          = aws_route_table.private[1]
  override_during = plan
  values          = { id = "rtb-00000000000000003" }
}

override_resource {
  target          = aws_cognito_user_pool_client.operator[0]
  override_during = plan
  values          = { id = "testoperatorclient" }
}

variables {
  application_runtime_enabled       = false
  notification_test_recipient_email = null
  notification_sender_email         = null
  token_trigger_package             = null
  token_trigger_sha256              = null
  token_trigger_pool_id             = null
  token_trigger_web_client_id       = null
  token_trigger_operator_client_id  = null
  attach_token_trigger              = false
  aws_account_id                    = "123456789012"
  eks_public_access_cidrs           = ["203.0.113.10/32"]
  eks_admin_principal_arns          = ["arn:aws:iam::123456789012:role/test-deployer"]
  frontend_origins                  = ["http://localhost:5173"]
  cognito_callback_urls             = ["http://localhost:5173/callback"]
  cognito_logout_urls               = ["http://localhost:5173/"]
  monthly_budget_usd                = 100
  budget_notification_emails        = ["team@example.com"]
  node_network_mode                 = "private"
}

run "private_compute_database_and_media" {
  command = plan
  assert {
    condition = (
      length(aws_subnet.private) == 2 &&
      toset(aws_eks_node_group.api.subnet_ids) == toset(aws_subnet.private[*].id) &&
      aws_subnet.private[0].availability_zone != aws_subnet.private[1].availability_zone &&
      alltrue([for subnet in aws_subnet.private : !subnet.map_public_ip_on_launch]) &&
      !aws_db_instance.this.publicly_accessible && aws_db_instance.this.storage_encrypted &&
      aws_db_instance.this.manage_master_user_password && aws_db_instance.this.backup_retention_period >= 7 &&
      aws_db_instance.this.deletion_protection &&
      aws_s3_bucket_public_access_block.media.block_public_acls &&
      aws_s3_bucket_public_access_block.media.block_public_policy &&
      aws_s3_bucket_public_access_block.media.ignore_public_acls &&
      aws_s3_bucket_public_access_block.media.restrict_public_buckets
    )
    error_message = "Compute and DB must use private networking across two AZs; DB credentials, backups, and media must be protected."
  }
  assert {
    condition = (
      one(aws_eks_cluster.this.vpc_config).endpoint_private_access &&
      !one(aws_eks_cluster.this.access_config).bootstrap_cluster_creator_admin_permissions &&
      toset(aws_eks_cluster.this.enabled_cluster_log_types) == toset(["api", "audit", "authenticator", "controllerManager", "scheduler"]) &&
      one(aws_launch_template.nodes.metadata_options).http_tokens == "required" &&
      one(aws_launch_template.nodes.metadata_options).http_put_response_hop_limit == 1 &&
      aws_vpc_security_group_egress_rule.nodes.cidr_ipv4 == var.vpc_cidr &&
      aws_vpc_security_group_egress_rule.node_https.ip_protocol == "tcp" &&
      aws_vpc_security_group_egress_rule.node_https.from_port == 443 &&
      aws_vpc_security_group_egress_rule.node_https.to_port == 443 &&
      length(aws_vpc_security_group_ingress_rule.control_plane_dns) == 2 &&
      alltrue([for rule in aws_vpc_security_group_ingress_rule.control_plane_dns : rule.from_port == 53 && rule.to_port == 53])
    )
    error_message = "EKS access must be explicit and audited; pods must not obtain node credentials through IMDS."
  }
  assert {
    condition = alltrue([
      for statements in values(local.workload_statements) : alltrue([
        for statement in statements : !contains(statement.Action, "*") && !contains(statement.Action, "secretsmanager:GetSecretValue")
      ])
    ])
    error_message = "Application IAM roles must not receive wildcard actions or the RDS administrator secret."
  }
  assert {
    condition = (
      length(aws_sqs_queue.jobs) == 3 && length(aws_sqs_queue.dead_letter) == 3 &&
      alltrue([for queue in aws_sqs_queue.jobs : queue.sqs_managed_sse_enabled]) &&
      length(aws_sns_topic_subscription.internal) == 2 &&
      alltrue([for subscription in aws_sns_topic_subscription.internal : subscription.raw_message_delivery])
    )
    error_message = "Every consumer needs an encrypted queue and DLQ; events must reach matching and notifications without an SNS envelope."
  }
  assert {
    condition = (
      !aws_cognito_user_pool_client.frontend.generate_secret &&
      aws_cognito_user_pool_client.frontend.allowed_oauth_flows == toset(["code"]) &&
      aws_cognito_user_pool_client.frontend.prevent_user_existence_errors == "ENABLED" &&
      aws_budgets_budget.dev.limit_amount == "100" &&
      length(aws_budgets_budget.dev.notification) == 2 &&
      length(aws_ses_email_identity.notifications) == 0
    )
    error_message = "Browser login needs a public code-flow client; cost alerts are mandatory, and SES must be explicitly configured."
  }
}

run "application_process_isolation_and_routing" {
  command = plan
  variables {
    application_runtime_enabled = true
  }
  assert {
    condition = (
      length(aws_cognito_user_pool_client.machine) == 4 &&
      alltrue([for client in aws_cognito_user_pool_client.machine : client.generate_secret && client.allowed_oauth_flows == toset(["client_credentials"])]) &&
      aws_cognito_user_pool_client.machine["item"].allowed_oauth_scopes == toset(["lostlink/members.check"]) &&
      aws_cognito_user_pool_client.machine["matching"].allowed_oauth_scopes == toset(["lostlink/items.index"]) &&
      aws_cognito_user_pool_client.frontend.id != aws_cognito_user_pool_client.operator[0].id
    )
    error_message = "Each machine must use a separate secret-bearing client with only its receiver's scopes."
  }
  assert {
    condition = (
      length(aws_sqs_queue.application) == 3 && length(aws_sqs_queue.application_dlq) == 3 &&
      jsondecode(aws_sns_topic_subscription.application["matching"].filter_policy).event_type == ["item.registered", "item.catalog_changed"] &&
      jsondecode(aws_sns_topic_subscription.application["item"].filter_policy).event_type == ["item.embedded", "item.embedding_failed"] &&
      alltrue([for sub in aws_sns_topic_subscription.application : sub.raw_message_delivery])
    )
    error_message = "Route current Phase 1 events to the correct consumers, including embedding results back to Item."
  }
  assert {
    condition = (
      local.storage_statements.upload[0].Resource == "${aws_s3_bucket.media.arn}/staging/*" &&
      local.storage_statements.seal[1].Resource == "${aws_s3_bucket.media.arn}/sealed/*" &&
      local.storage_statements.embed[0].Action == ["s3:GetObject"] &&
      alltrue([for process in local.process_contracts : !contains(process.db, "venue/migration") && !contains(process.db, "item/migration")]) &&
      length(local.process_contracts) == 18 &&
      one(aws_s3_bucket_cors_configuration.media.cors_rule).allowed_methods == toset(["POST"])
    )
    error_message = "Runtime identities cannot receive migration credentials; only the Item sealer writes sealed photos, and CORS permits POST uploads."
  }
}

run "private_trigger_access_token_configuration" {
  command = plan
  variables {
    application_runtime_enabled      = true
    node_network_mode                = "public"
    token_trigger_package            = "build/mock-only.zip"
    token_trigger_sha256             = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    token_trigger_pool_id            = "ap-southeast-1_test"
    token_trigger_web_client_id      = "testwebclient"
    token_trigger_operator_client_id = "testoperatorclient"
    attach_token_trigger             = true
  }
  assert {
    condition = (
      one(one(aws_cognito_user_pool.this.lambda_config).pre_token_generation_config).lambda_version == "V2_0" &&
      aws_cognito_user_pool.this.user_pool_tier == "ESSENTIALS" &&
      length(aws_nat_gateway.this) == 0 &&
      length(aws_vpc_endpoint.secrets[0].subnet_ids) == 1 &&
      aws_vpc_endpoint.secrets[0].private_dns_enabled &&
      toset(one(aws_lambda_function.token_trigger[0].vpc_config).subnet_ids) == toset(aws_subnet.private[*].id) &&
      aws_vpc_security_group_ingress_rule.database_trigger[0].from_port == 5432 &&
      aws_vpc_security_group_egress_rule.trigger_secrets[0].to_port == 443 &&
      !contains(keys(one(aws_lambda_function.token_trigger[0].environment).variables), "IDENTITY_DATABASE_URL") &&
      aws_lambda_permission.cognito[0].source_account == var.aws_account_id
    )
    error_message = "Access-token customization must use V2, private database/secret access without NAT, no credential in the Lambda environment, and a pool-scoped invocation permission."
  }
}

run "per_az_nat_and_optional_email" {
  command = plan
  variables {
    single_nat_gateway        = false
    notification_sender_email = "sender@example.com"
  }
  assert {
    condition     = length(aws_nat_gateway.this) == 2 && length(aws_ses_email_identity.notifications) == 1
    error_message = "Per-AZ egress must create two NAT gateways; configuring a sender must create its SES identity."
  }
}

run "single_cpu_demo_without_nat" {
  command = plan
  variables {
    node_network_mode = "public"
    node_desired_size = 1
    node_max_size     = 1
  }
  assert {
    condition = (
      length(aws_nat_gateway.this) == 0 && length(aws_eip.nat) == 0 && length(aws_route.nat) == 0 &&
      alltrue([for subnet in aws_subnet.public : subnet.map_public_ip_on_launch]) &&
      alltrue([for subnet in aws_subnet.private : !subnet.map_public_ip_on_launch]) &&
      toset(aws_eks_node_group.api.subnet_ids) == toset(aws_subnet.public[*].id) &&
      one(aws_eks_node_group.api.scaling_config).min_size == 1 &&
      one(aws_eks_node_group.api.scaling_config).desired_size == 1 &&
      one(aws_eks_node_group.api.scaling_config).max_size == 1 &&
      toset(aws_eks_node_group.api.instance_types) == toset(["t3.large"]) &&
      !aws_db_instance.this.publicly_accessible &&
      length(aws_vpc_endpoint.s3.route_table_ids) == 3 &&
      aws_vpc_security_group_ingress_rule.node_internal.cidr_ipv4 == null &&
      aws_vpc_security_group_ingress_rule.node_internal.cidr_ipv6 == null &&
      aws_vpc_security_group_ingress_rule.node_control_plane.cidr_ipv4 == null &&
      aws_vpc_security_group_ingress_rule.node_control_plane.cidr_ipv6 == null
    )
    error_message = "The CPU demo needs one public-subnet node with scoped SG ingress, zero NAT/EIPs, S3 endpoint routing, and a private database."
  }
}

run "reject_zero_nodes" {
  command = plan
  variables {
    node_desired_size = 0
  }
  expect_failures = [var.node_desired_size]
}

run "reject_invalid_node_network" {
  command = plan
  variables {
    node_network_mode = "unknown"
  }
  expect_failures = [var.node_network_mode]
}

run "reject_world_open_cluster" {
  command = plan
  variables {
    eks_public_access_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.eks_public_access_cidrs]
}

run "reject_cross_account_admin" {
  command = plan
  variables {
    eks_admin_principal_arns = ["arn:aws:iam::999999999999:role/admin"]
  }
  expect_failures = [var.eks_admin_principal_arns]
}

run "reject_single_az" {
  command = plan
  variables {
    availability_zones = ["ap-southeast-1a", "ap-southeast-1a"]
  }
  expect_failures = [var.availability_zones]
}

run "reject_wildcard_browser_origin" {
  command = plan
  variables {
    frontend_origins = ["*"]
  }
  expect_failures = [var.frontend_origins]
}

run "reject_missing_budget_owner" {
  command = plan
  variables {
    budget_notification_emails = []
  }
  expect_failures = [var.budget_notification_emails]
}

run "reject_unbudgeted_deployment" {
  command = plan
  variables {
    monthly_budget_usd = 0
  }
  expect_failures = [var.monthly_budget_usd]
}

run "reject_sts_session_as_admin" {
  command = plan
  variables {
    eks_admin_principal_arns = ["arn:aws:sts::123456789012:assumed-role/admin/session"]
  }
  expect_failures = [var.eks_admin_principal_arns]
}

run "reject_mismatched_postgres_family" {
  command = plan
  variables {
    postgres_engine_version = "17"
  }
  expect_failures = [var.postgres_engine_version]
}

run "c3_private_gateway_and_controller_identity" {
  command = plan
  override_resource {
    target          = aws_eks_cluster.this
    override_during = plan
    values = {
      identity = [{ oidc = [{ issuer = "https://oidc.eks.ap-southeast-1.amazonaws.com/id/test" }] }]
    }
  }
  override_resource {
    target          = aws_iam_openid_connect_provider.eks
    override_during = plan
    values = {
      arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.ap-southeast-1.amazonaws.com/id/test"
    }
  }
  variables {
    c3_enabled        = true
    node_desired_size = 2
    node_max_size     = 5
  }
  assert {
    condition = (
      length(aws_iam_role.alb_controller) == 1 &&
      length(aws_cloudfront_distribution.dev) == 0 &&
      aws_ecr_repository.frontend[0].image_tag_mutability == "IMMUTABLE" &&
      aws_vpc_security_group_ingress_rule.c3_cloudfront[0].cidr_ipv4 == null &&
      aws_vpc_security_group_ingress_rule.c3_cloudfront[0].from_port == 80 &&
      aws_vpc_security_group_ingress_rule.c3_gateway[0].from_port == 8080 &&
      aws_vpc_security_group_ingress_rule.c3_gateway[0].to_port == 8080 &&
      jsondecode(aws_iam_role.alb_controller[0].assume_role_policy).Statement[0].Condition.StringEquals["${local.oidc_host}:sub"] == "system:serviceaccount:kube-system:aws-load-balancer-controller"
    )
    error_message = "C3 requires an isolated controller identity, immutable frontend images and a scoped private gateway boundary."
  }
}
