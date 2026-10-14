# Tudo simulado. Nenhum apply real nem chamada à conta AWS.
mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a"] }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-00000000000000000" }
  }
  mock_data "aws_s3_bucket" {
    defaults = { region = "us-east-1", arn = "arn:aws:s3:::fixture-nextcloud-data-test" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
}

variables {
  aws_account_id     = "000000000000"
  aws_region         = "us-east-1"
  project_name       = "fixture"
  environment        = "test"
  data_bucket_name   = "fixture-nextcloud-data-test"
  ssh_public_key     = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKV0vgZRgTTKRauycgaPJuzcfIdULKKejVEQSZ7RAjjP fixture-public-key-only"
  ssh_allowed_cidrs  = ["192.0.2.1/32"]
  http_allowed_cidrs = ["192.0.2.1/32"]
}

run "runtime_defaults" {
  command = plan
  assert {
    condition     = aws_instance.host.metadata_options[0].http_put_response_hop_limit == 2 && aws_instance.host.iam_instance_profile == aws_iam_instance_profile.host.name
    error_message = "Containers devem usar profile EC2 com IMDSv2 em bridge."
  }
  assert {
    condition     = aws_instance.host.associate_public_ip_address && aws_instance.host.metadata_options[0].http_tokens == "required"
    error_message = "Exigir IPv4 dinâmico e IMDSv2."
  }
  assert {
    condition     = aws_instance.host.root_block_device[0].encrypted && aws_instance.host.root_block_device[0].delete_on_termination && aws_instance.host.root_block_device[0].volume_size == var.root_volume_size_gib
    error_message = "Disco deve ser pequeno, criptografado e descartável."
  }
  assert {
    condition     = aws_instance.host.credit_specification[0].cpu_credits == "standard" && aws_instance.host.instance_type == var.instance_type
    error_message = "Manter default econômico, sem créditos ilimitados."
  }
}

run "reject_public_ssh" {
  command = plan
  variables {
    aws_account_id    = "000000000000"
    ssh_allowed_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.ssh_allowed_cidrs]
}

run "grafana_administrative_ipv4_only" {
  command = plan
  variables {
    http_allowed_cidrs = ["192.0.2.1/32", "198.51.100.0/24"]
  }
  assert {
    condition = length(aws_vpc_security_group_ingress_rule.grafana) == length(var.http_allowed_cidrs) && alltrue([
      for cidr, rule in aws_vpc_security_group_ingress_rule.grafana :
      rule.cidr_ipv4 == cidr && rule.cidr_ipv6 == null && rule.from_port == 3000 && rule.to_port == 3000 && rule.ip_protocol == "tcp"
    ])
    error_message = "Grafana deve compartilhar somente os CIDRs administrativos IPv4, TCP 3000."
  }
}

run "reject_public_http_grafana" {
  command = plan
  variables { http_allowed_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.http_allowed_cidrs]
}

run "reject_ipv6_http_grafana" {
  command = plan
  variables { http_allowed_cidrs = ["::/0"] }
  expect_failures = [var.http_allowed_cidrs]
}
