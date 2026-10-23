variable "aws_account_id" {
  type        = string
  description = "Deployment is restricted to this account."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Provide a 12-digit account ID."
  }
}

variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

variable "availability_zones" {
  type        = list(string)
  description = "Exactly two distinct AZs in the deployment region."
  default     = ["ap-southeast-1a", "ap-southeast-1b"]
  validation {
    condition     = length(var.availability_zones) == 2 && length(distinct(var.availability_zones)) == 2 && alltrue([for az in var.availability_zones : startswith(az, var.aws_region)])
    error_message = "Choose two distinct availability zones in aws_region."
  }
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
  validation {
    condition     = can(cidrsubnet(var.vpc_cidr, 8, 12))
    error_message = "Provide an IPv4 CIDR with space for the /24-equivalent subnets."
  }
}

variable "node_network_mode" {
  type        = string
  default     = "public"
  description = "Demo uses public-subnet nodes with HTTPS egress and no public ingress/NAT. Private mode uses NAT gateways."
  validation {
    condition     = contains(["public", "private"], var.node_network_mode)
    error_message = "node_network_mode must be public or private."
  }
}

variable "single_nat_gateway" {
  type        = bool
  default     = true
  description = "Only for private node networking: one NAT shared across AZs; false creates one per AZ. Public mode creates none."
}

variable "eks_public_access_cidrs" {
  type        = list(string)
  description = "Administrator public IPv4 CIDRs; use your actual egress IP/32."
  validation {
    condition     = length(var.eks_public_access_cidrs) > 0 && alltrue([for cidr in var.eks_public_access_cidrs : can(cidrnetmask(cidr)) && try(tonumber(split("/", cidr)[1]) >= 24, false)])
    error_message = "Use explicit IPv4 CIDRs of /24 or narrower; public 0.0.0.0/0 access is rejected."
  }
}

variable "eks_admin_principal_arns" {
  type        = set(string)
  description = "Existing IAM user/role ARNs to grant cluster administration; not STS session ARNs."
  validation {
    condition     = length(var.eks_admin_principal_arns) > 0 && alltrue([for arn in var.eks_admin_principal_arns : can(regex("^arn:aws:iam::${var.aws_account_id}:(role|user)/.+$", arn))])
    error_message = "Provide at least one IAM role/user ARN from aws_account_id, not a root or assumed-role session ARN."
  }
}

variable "kubernetes_version" {
  type    = string
  default = "1.36"
  validation {
    condition     = can(regex("^1\\.[0-9]+$", var.kubernetes_version))
    error_message = "Use a minor version such as 1.36; verify EKS regional availability before applying."
  }
}

variable "node_instance_types" {
  type    = list(string)
  default = ["t3.large"]
}

variable "node_desired_size" {
  type    = number
  default = 1
  validation {
    condition     = var.node_desired_size >= 1 && var.node_desired_size <= var.node_max_size && floor(var.node_desired_size) == var.node_desired_size
    error_message = "Run at least one node, with desired size no greater than node_max_size."
  }
}

variable "node_max_size" {
  type    = number
  default = 1
  validation {
    condition     = var.node_max_size >= 1 && floor(var.node_max_size) == var.node_max_size
    error_message = "node_max_size must be an integer of at least one."
  }
}

variable "addon_versions" {
  type        = map(string)
  default     = {}
  description = "Optional tested EKS addon versions keyed by addon name; otherwise AWS selects compatible defaults."
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "postgres_engine_version" {
  type        = string
  default     = "16"
  description = "PostgreSQL major/minor supported by RDS in this region; minor upgrades are automatic."
  validation {
    condition     = can(regex("^16(\\.[0-9]+)?$", var.postgres_engine_version))
    error_message = "This stack's parameter group targets PostgreSQL 16."
  }
}

variable "database_deletion_protection" {
  type    = bool
  default = true
}

variable "frontend_origins" {
  type        = list(string)
  description = "Exact frontend origins for S3 CORS; localhost is allowed for development."
  validation {
    condition     = length(var.frontend_origins) > 0 && alltrue([for origin in var.frontend_origins : can(regex("^https://[^/*]+$|^http://(localhost|127\\.0\\.0\\.1)(:[0-9]+)?$", origin))])
    error_message = "Use exact HTTPS origins or localhost/127.0.0.1 HTTP development origins, without wildcard or path."
  }
}

variable "cognito_callback_urls" {
  type = list(string)
  validation {
    condition     = length(var.cognito_callback_urls) > 0 && alltrue([for url in var.cognito_callback_urls : can(regex("^https://[^*]+$|^http://(localhost|127\\.0\\.0\\.1)(:[0-9]+)?(/[^*]*)?$", url))])
    error_message = "Specify exact HTTPS callback URLs, or localhost development URLs."
  }
}

variable "cognito_logout_urls" {
  type = list(string)
  validation {
    condition     = length(var.cognito_logout_urls) > 0 && alltrue([for url in var.cognito_logout_urls : can(regex("^https://[^*]+$|^http://(localhost|127\\.0\\.0\\.1)(:[0-9]+)?(/[^*]*)?$", url))])
    error_message = "Specify exact HTTPS logout URLs, or localhost development URLs."
  }
}

variable "monthly_budget_usd" {
  type        = number
  description = "Team-approved monthly account-wide cost alarm; not an automatic spending cap."
  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "Set a positive team-approved monthly budget in USD."
  }
}

variable "budget_notification_emails" {
  type = set(string)
  validation {
    condition     = length(var.budget_notification_emails) > 0 && length(var.budget_notification_emails) <= 10 && alltrue([for email in var.budget_notification_emails : can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", email))])
    error_message = "Provide 1 to 10 actual budget notification email addresses."
  }
}

variable "notification_sender_email" {
  type        = string
  default     = null
  nullable    = true
  description = "Optional SES sender. Its owner must complete email verification; SES sandbox recipients also need verification."
  validation {
    condition     = var.notification_sender_email == null ? true : can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", var.notification_sender_email))
    error_message = "Provide an email address or leave null."
  }
}
