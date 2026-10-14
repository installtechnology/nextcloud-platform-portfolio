# Pinned copy of ../runtime/main.tf; existing operational root is unchanged.
locals {
  name            = "${var.project_name}-${var.environment}"
  data_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.data_bucket_name}"
}

data "aws_partition" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}
data "aws_ssm_parameter" "ubuntu" {
  name = var.ubuntu_ami_ssm_parameter
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.subnet_cidr
  availability_zone       = coalesce(var.availability_zone, sort(data.aws_availability_zones.available.names)[0])
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.name}-public" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-public" }
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "host" {
  name_prefix = "${local.name}-"
  description = "HTTP e SSH limitados aos CIDRs do operador"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = local.name }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each          = var.ssh_allowed_cidrs
  security_group_id = aws_security_group.host.id
  description       = "SSH administrativo"
  cidr_ipv4         = each.value
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  for_each          = var.http_allowed_cidrs
  security_group_id = aws_security_group.host.id
  description       = "HTTP de laboratorio"
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "outbound" {
  security_group_id = aws_security_group.host.id
  description       = "Pacotes Ubuntu, imagens de containers e APIs AWS"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "grafana" {
  for_each          = var.http_allowed_cidrs
  security_group_id = aws_security_group.host.id
  description       = "Grafana HTTP administrativo"
  cidr_ipv4         = each.value
  from_port         = 3000
  to_port           = 3000
  ip_protocol       = "tcp"
}

resource "aws_key_pair" "operator" {
  key_name_prefix = "${local.name}-"
  public_key      = trimspace(var.ssh_public_key)
}

resource "aws_instance" "host" {
  ami                         = nonsensitive(data.aws_ssm_parameter.ubuntu.value)
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.host.id]
  key_name                    = aws_key_pair.operator.key_name
  iam_instance_profile        = aws_iam_instance_profile.host.name

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gib
    encrypted             = true
    delete_on_termination = true
    tags                  = { Name = "${local.name}-root" }
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2 # Necessário para futura aplicação em rede bridge Docker.
    instance_metadata_tags      = "disabled"
  }
  credit_specification {
    cpu_credits = "standard" # Evita cobrança adicional de créditos unlimited em T3.
  }
  tags = merge({ Name = local.name }, var.creation_grant == null ? {} : { ReprovisionGrant = var.creation_grant })

  depends_on = [aws_route.internet, aws_route_table_association.public, aws_iam_role_policy.storage]
}
