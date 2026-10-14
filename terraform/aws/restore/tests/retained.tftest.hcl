mock_provider "aws" {
  override_during = apply
  mock_resource "aws_s3_bucket" {
    defaults = {
      arn      = "arn:aws:s3:::fixture-destination"
      id       = "fixture-destination"
      tags_all = { Project = "fixture", Environment = "restore", ManagedBy = "Terraform", Portfolio = "synthetic" }
    }
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
run "retain_storage_only" {
  command = apply
  plan_options {
    target = [terraform_data.isolation, aws_s3_bucket.data, aws_s3_bucket_public_access_block.data, aws_s3_bucket_ownership_controls.data, aws_s3_bucket_versioning.data, aws_s3_bucket_server_side_encryption_configuration.data, aws_s3_bucket_policy.tls]
  }
  variables { tags = { Portfolio = "synthetic" } }
  assert {
    condition     = !aws_s3_bucket.data.force_destroy && aws_s3_bucket_versioning.data.versioning_configuration[0].status == "Enabled"
    error_message = "Persistent protections must survive runtime-only creation."
  }
}
run "create_runtime_preserving_storage" {
  command = plan
  variables {
    tags           = { Portfolio = "synthetic" }
    creation_grant = "fixture-grant-v2"
  }
  assert {
    condition     = aws_s3_bucket.data.id == run.retain_storage_only.destination_data_bucket && aws_s3_bucket.data.tags_all["Portfolio"] == "synthetic" && !contains(keys(aws_s3_bucket.data.tags_all), "ReprovisionGrant")
    error_message = "Retained storage identity and tags must remain unchanged."
  }
  assert {
    condition     = aws_instance.host.tags["ReprovisionGrant"] == "fixture-grant-v2"
    error_message = "Grant must identify only the new EC2, not retag persistent resources."
  }
}
