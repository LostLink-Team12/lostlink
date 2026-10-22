terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
  default_tags {
    tags = { Project = "lostlink", Environment = "dev", ManagedBy = "terraform" }
  }
}

locals {
  name        = "lostlink-dev"
  account_arn = "arn:aws:iam::${var.aws_account_id}"
  oidc_host   = replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")
  services = toset([
    "service-template", "venue-service", "item-service", "claim-service",
    "notification-service", "embedding-worker", "matching-service", "delivery-service"
  ])
}
