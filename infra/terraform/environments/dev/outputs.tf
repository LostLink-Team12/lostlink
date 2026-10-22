output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "region" {
  value = var.aws_region
}

output "database" {
  description = "Master secret ARN is for the one-off migration operator; give applications separate SQL users."
  value = {
    host              = aws_db_instance.this.address
    port              = aws_db_instance.this.port
    name              = aws_db_instance.this.db_name
    master_secret_arn = aws_db_instance.this.master_user_secret[0].secret_arn
    sslmode           = "verify-full"
  }
}

output "media_bucket" {
  value = aws_s3_bucket.media.bucket
}

output "ecr_repositories" {
  value = { for service, repository in aws_ecr_repository.services : service => repository.repository_url }
}

output "queue_urls" {
  value = { for name, queue in aws_sqs_queue.jobs : name => queue.url }
}

output "dead_letter_queue_urls" {
  value = { for name, queue in aws_sqs_queue.dead_letter : name => queue.url }
}

output "event_topic_arns" {
  value = { for event, topic in aws_sns_topic.events : event => topic.arn }
}

output "cognito" {
  value = {
    user_pool_id = aws_cognito_user_pool.this.id
    client_id    = aws_cognito_user_pool_client.frontend.id
    issuer       = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.this.id}"
    login_domain = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com"
  }
}

output "workload_role_arns" {
  description = "C3 Helm service accounts in namespace lostlink need eks.amazonaws.com/role-arn annotations."
  value       = { for service, role in aws_iam_role.workload : service => role.arn }
}

output "notification_sender_email" {
  value = var.notification_sender_email
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  description = "Internet-facing ALB subnets; demo worker nodes also use these subnets."
  value       = aws_subnet.public[*].id
}

output "node_security_group_id" {
  description = "C3 load balancer controller manages narrowly scoped backend ingress on this group."
  value       = aws_security_group.nodes.id
}
