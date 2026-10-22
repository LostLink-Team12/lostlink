resource "aws_iam_role" "cluster" {
  name = "${local.name}-eks-cluster"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "eks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role_policy" "cluster_encryption" {
  role = aws_iam_role.cluster.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = ["kms:DescribeKey", "kms:CreateGrant"], Resource = aws_kms_key.dev.arn
    }]
  })
}

resource "aws_cloudwatch_log_group" "eks" {
  name              = "/aws/eks/${local.name}/cluster"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.dev.arn
}

# Dev operators connect from an explicit IPv4 allowlist; variable validation
# rejects world-open access. Private endpoint serves nodes. See README exceptions.
# trivy:ignore:AVD-AWS-0040
resource "aws_eks_cluster" "this" {
  name                      = local.name
  role_arn                  = aws_iam_role.cluster.arn
  version                   = var.kubernetes_version
  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }
  vpc_config {
    subnet_ids              = aws_subnet.private[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    # Demo operator access is a validated /24-or-narrower allowlist, currently
    # one /32. This rule flags any public CIDR, including that intentional IP.
    # trivy:ignore:AVD-AWS-0041
    public_access_cidrs = var.eks_public_access_cidrs
  }
  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.dev.arn
    }
  }
  upgrade_policy {
    support_type = "STANDARD"
  }
  depends_on = [aws_iam_role_policy_attachment.cluster, aws_iam_role_policy.cluster_encryption, aws_cloudwatch_log_group.eks]
}

resource "aws_eks_access_entry" "admin" {
  for_each      = var.eks_admin_principal_arns
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "admin" {
  for_each      = var.eks_admin_principal_arns
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.admin[each.key].principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
}

resource "aws_iam_role" "nodes" {
  name = "${local.name}-eks-nodes"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "nodes" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryReadOnly", "AmazonEKS_CNI_Policy"])
  role       = aws_iam_role.nodes.name
  policy_arn = "arn:aws:iam::aws:policy/${each.value}"
}

resource "aws_launch_template" "nodes" {
  name_prefix = "${local.name}-"
  # Do not attach the EKS primary SG's default unrestricted egress to nodes.
  vpc_security_group_ids = [aws_security_group.nodes.id]
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 30
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name}-node", Project = "lostlink", Environment = "dev" }
  }
}

resource "aws_eks_node_group" "api" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "general"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = var.node_network_mode == "public" ? aws_subnet.public[*].id : aws_subnet.private[*].id
  instance_types  = var.node_instance_types
  ami_type        = "AL2023_x86_64_STANDARD"
  capacity_type   = "ON_DEMAND"
  scaling_config {
    min_size     = 1
    desired_size = var.node_desired_size
    max_size     = var.node_max_size
  }
  update_config {
    max_unavailable = 1
  }
  launch_template {
    id      = aws_launch_template.nodes.id
    version = aws_launch_template.nodes.latest_version
  }
  labels = { workload = "general" }
  depends_on = [
    aws_iam_role_policy_attachment.nodes, aws_route.nat, aws_route_table_association.private,
    aws_route.internet, aws_route_table_association.public,
    aws_vpc_security_group_ingress_rule.cluster_api, aws_vpc_security_group_ingress_rule.node_control_plane,
    aws_vpc_security_group_ingress_rule.node_internal, aws_vpc_security_group_egress_rule.node_https,
    aws_vpc_security_group_egress_rule.nodes, aws_vpc_security_group_ingress_rule.control_plane_dns
  ]
}

resource "aws_eks_addon" "base" {
  for_each                    = toset(["vpc-cni", "coredns", "kube-proxy"])
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.value
  addon_version               = lookup(var.addon_versions, each.value, null)
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  depends_on                  = [aws_eks_node_group.api]
}

resource "aws_iam_openid_connect_provider" "eks" {
  url            = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]
}
