mock_provider "aws" {
  override_during = plan
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::fixture-destination", id = "fixture-destination" }
  }
  mock_data "aws_partition" { defaults = { partition = "aws", dns_suffix = "amazonaws.com" } }
  mock_data "aws_availability_zones" { defaults = { names = ["us-east-1a"] } }
  mock_data "aws_ssm_parameter" { defaults = { value = "ami-00000000000000000" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{}" } }
}
variables {
  aws_account_id               = "000000000000"
  aws_region                   = "us-east-1"
  project_name                 = "fixture"
  environment                  = "restore"
  source_environment           = "test"
  source_data_bucket_name      = "fixture-source-data"
  data_bucket_name             = "fixture-destination"
  backups_bucket_name          = "fixture-backups"
  state_bucket_name            = "fixture-state"
  ec2_permissions_boundary_arn = "arn:aws:iam::000000000000:policy/fixture-restore-ec2-boundary"
  ssh_public_key               = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKV0vgZRgTTKRauycgaPJuzcfIdULKKejVEQSZ7RAjjP synthetic"
  ssh_allowed_cidrs            = ["192.0.2.1/32"]
  http_allowed_cidrs           = ["192.0.2.1/32"]
}
run "independent_destination" {
  command = plan
  assert {
    condition     = !aws_s3_bucket.data.force_destroy && aws_s3_bucket_versioning.data.versioning_configuration[0].status == "Enabled" && aws_s3_bucket_ownership_controls.data.rule[0].object_ownership == "BucketOwnerPreferred"
    error_message = "Preserve data, versions and Nextcloud ACL compatibility."
  }
  assert {
    condition     = jsondecode(aws_s3_bucket_policy.tls.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    error_message = "TLS mandatory."
  }
}
run "runtime_controls" {
  command = plan
  assert {
    condition     = aws_instance.host.instance_type == "t3.medium" && aws_instance.host.root_block_device[0].encrypted && aws_instance.host.root_block_device[0].volume_type == "gp3" && aws_instance.host.metadata_options[0].http_tokens == "required" && aws_instance.host.metadata_options[0].http_put_response_hop_limit == 2 && aws_iam_role.host.permissions_boundary == var.ec2_permissions_boundary_arn
    error_message = "Pinned runtime controls and destination-only host boundary."
  }
}
run "reject_source_destination" {
  command = plan
  variables { data_bucket_name = "fixture-source-data" }
  expect_failures = [terraform_data.isolation]
}
run "reject_missing_boundary" {
  command = plan
  variables { ec2_permissions_boundary_arn = null }
  expect_failures = [terraform_data.isolation]
}
