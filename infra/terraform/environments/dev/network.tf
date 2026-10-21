locals {
  nat_gateway_count = var.node_network_mode == "private" ? (var.single_nat_gateway ? 1 : 2) : 0
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = local.name }
  depends_on           = [aws_budgets_budget.dev]
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
}

# Public addresses provide outbound access without a NAT in the demo. Node SGs
# admit only other nodes and the EKS control plane; there is no public ingress.
# trivy:ignore:AVD-AWS-0164
resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  availability_zone       = var.availability_zones[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  map_public_ip_on_launch = var.node_network_mode == "public"
  tags = {
    Name                     = "${local.name}-public-${count.index}"
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_subnet" "private" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  availability_zone       = var.availability_zones[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 10 + count.index)
  map_public_ip_on_launch = false
  tags = {
    Name                              = "${local.name}-private-${count.index}"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  count  = local.nat_gateway_count
  domain = "vpc"
}

resource "aws_nat_gateway" "this" {
  count         = local.nat_gateway_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  depends_on    = [aws_internet_gateway.this, aws_route.internet]
}

resource "aws_route_table" "private" {
  count  = 2
  vpc_id = aws_vpc.this.id
}

resource "aws_route" "nat" {
  count                  = var.node_network_mode == "private" ? 2 : 0
  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.node_network_mode == "public" ? concat(aws_route_table.private[*].id, [aws_route_table.public.id]) : aws_route_table.private[*].id
}

resource "aws_security_group" "nodes" {
  name_prefix = "${local.name}-nodes-"
  description = "Additional identity for EKS nodes, used by the database ingress rule"
  vpc_id      = aws_vpc.this.id
  tags        = { "kubernetes.io/cluster/${local.name}" = "owned" }
}

resource "aws_vpc_security_group_egress_rule" "nodes" {
  security_group_id = aws_security_group.nodes.id
  description       = "Private cluster, pod and database traffic"
  ip_protocol       = "-1"
  cidr_ipv4         = var.vpc_cidr
}

# AWS APIs/ECR/model registries have public, changing IPs; only TLS is allowed.
# Tests pin this exception to TCP/443. See README scanner exceptions.
# trivy:ignore:AVD-AWS-0104
resource "aws_vpc_security_group_egress_rule" "node_https" {
  security_group_id = aws_security_group.nodes.id
  description       = "TLS to ECR, AWS APIs and CLIP model registries"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "node_internal" {
  security_group_id            = aws_security_group.nodes.id
  referenced_security_group_id = aws_security_group.nodes.id
  description                  = "Node and pod traffic within this group"
  ip_protocol                  = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "node_control_plane" {
  security_group_id            = aws_security_group.nodes.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  description                  = "Control plane to kubelet and admission webhooks"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 65535
}

resource "aws_vpc_security_group_ingress_rule" "cluster_api" {
  security_group_id            = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  referenced_security_group_id = aws_security_group.nodes.id
  description                  = "Nodes and pods to the private Kubernetes API"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "control_plane_dns" {
  for_each                     = toset(["tcp", "udp"])
  security_group_id            = aws_security_group.nodes.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  description                  = "Control plane DNS access to CoreDNS"
  ip_protocol                  = each.key
  from_port                    = 53
  to_port                      = 53
}

resource "aws_cloudwatch_log_group" "vpc" {
  name              = "/aws/vpc/${local.name}"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.dev.arn
}

resource "aws_iam_role" "flow_logs" {
  name = "${local.name}-vpc-flow-logs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = "vpc-flow-logs.amazonaws.com" }, Action = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.aws_account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:ec2:${var.aws_region}:${var.aws_account_id}:vpc-flow-log/*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "flow_logs" {
  role = aws_iam_role.flow_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "${aws_cloudwatch_log_group.vpc.arn}:*"
      },
      { Effect = "Allow", Action = ["logs:DescribeLogGroups"], Resource = "*" }
    ]
  })
}

resource "aws_flow_log" "this" {
  vpc_id          = aws_vpc.this.id
  traffic_type    = "ALL"
  iam_role_arn    = aws_iam_role.flow_logs.arn
  log_destination = aws_cloudwatch_log_group.vpc.arn
  depends_on      = [aws_iam_role_policy.flow_logs]
}
