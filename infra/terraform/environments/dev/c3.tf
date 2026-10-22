variable "c3_enabled" {
  type    = bool
  default = false
}

variable "c3_alb_dns_name" {
  type        = string
  default     = null
  description = "Set after the C3 Ingress has provisioned its ALB; enables the dev HTTPS distribution."
}

variable "c3_alb_arn" {
  type    = string
  default = null
}

variable "c3_origin_token" {
  type        = string
  default     = null
  sensitive   = true
  description = "Random CloudFront origin header, supplied via TF_VAR from ignored operator storage."
  validation {
    condition     = var.c3_origin_token == null ? true : length(var.c3_origin_token) >= 32
    error_message = "Use at least 32 random characters."
  }
}

locals {
  c3_edge_enabled  = var.c3_enabled && var.c3_alb_dns_name != null
  c3_origin        = local.c3_edge_enabled ? "https://${aws_cloudfront_distribution.dev[0].domain_name}" : null
  frontend_origins = concat(var.frontend_origins, local.c3_edge_enabled ? [local.c3_origin] : [])
  callback_urls = concat(var.cognito_callback_urls, local.c3_edge_enabled ? [
    "${local.c3_origin}/owner-app/", "${local.c3_origin}/venue-portal/"
  ] : [])
  logout_urls = concat(var.cognito_logout_urls, local.c3_edge_enabled ? [
    "${local.c3_origin}/owner-app/", "${local.c3_origin}/venue-portal/"
  ] : [])
}

resource "aws_iam_role" "alb_controller" {
  count = var.c3_enabled ? 1 : 0
  name  = "${local.name}-alb-controller"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = "system:serviceaccount:kube-system:aws-load-balancer-controller"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "alb_controller" {
  count  = var.c3_enabled ? 1 : 0
  role   = aws_iam_role.alb_controller[0].id
  policy = file("${path.module}/alb-controller-policy.json")
}

resource "aws_ecr_repository" "frontend" {
  count                = var.c3_enabled ? 1 : 0
  name                 = "lostlink/frontend"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

data "aws_ec2_managed_prefix_list" "cloudfront" {
  count = var.c3_enabled ? 1 : 0
  name  = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "c3_alb" {
  count       = var.c3_enabled ? 1 : 0
  name        = "${local.name}-frontend-alb"
  description = "CloudFront origin traffic to the public frontend gateway only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "c3_cloudfront" {
  count             = var.c3_enabled ? 1 : 0
  security_group_id = aws_security_group.c3_alb[0].id
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront[0].id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  description       = "CloudFront managed origin prefix list"
}

resource "aws_vpc_security_group_egress_rule" "c3_gateway" {
  count                        = var.c3_enabled ? 1 : 0
  security_group_id            = aws_security_group.c3_alb[0].id
  referenced_security_group_id = aws_security_group.nodes.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_ingress_rule" "c3_gateway" {
  count                        = var.c3_enabled ? 1 : 0
  security_group_id            = aws_security_group.nodes.id
  referenced_security_group_id = aws_security_group.c3_alb[0].id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_cloudfront_vpc_origin" "dev" {
  count = local.c3_edge_enabled ? 1 : 0
  vpc_origin_endpoint_config {
    name                   = "${local.name}-alb"
    arn                    = var.c3_alb_arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
}

resource "aws_cloudfront_distribution" "dev" {
  count           = local.c3_edge_enabled ? 1 : 0
  enabled         = true
  comment         = "LostLink C3 dev frontend and public API"
  price_class     = "PriceClass_100"
  http_version    = "http2and3"
  is_ipv6_enabled = true
  origin {
    domain_name = var.c3_alb_dns_name
    origin_id   = "dev-alb"
    custom_header {
      name  = "X-LostLink-Origin"
      value = var.c3_origin_token
    }
    vpc_origin_config {
      vpc_origin_id       = aws_cloudfront_vpc_origin.dev[0].id
      origin_read_timeout = 60
    }
  }
  default_cache_behavior {
    target_origin_id       = "dev-alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    # CachingDisabled; authenticated API responses and config never enter cache.
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled[0].id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.viewer[0].id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security[0].id
    compress                   = true
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate { cloudfront_default_certificate = true }
  lifecycle {
    precondition {
      condition     = var.c3_origin_token != null
      error_message = "CloudFront requires a random c3_origin_token."
    }
  }
}

data "aws_cloudfront_cache_policy" "disabled" {
  count = local.c3_edge_enabled ? 1 : 0
  name  = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "viewer" {
  count = local.c3_edge_enabled ? 1 : 0
  name  = "Managed-AllViewerExceptHostHeader"
}

data "aws_cloudfront_response_headers_policy" "security" {
  count = local.c3_edge_enabled ? 1 : 0
  name  = "Managed-SecurityHeadersPolicy"
}

output "c3" {
  value = var.c3_enabled ? {
    alb_controller_role_arn = aws_iam_role.alb_controller[0].arn
    alb_security_group_id   = aws_security_group.c3_alb[0].id
    frontend_repository     = aws_ecr_repository.frontend[0].repository_url
    frontend_origin         = local.c3_origin
  } : null
}
