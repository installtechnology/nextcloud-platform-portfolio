mock_provider "aws" {
  override_during = plan
  mock_data "aws_partition" { defaults = { partition = "aws", dns_suffix = "amazonaws.com" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{}" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::000000000000:policy/fixture-boundary" } }
}
variables {
  aws_account_id      = "000000000000"
  aws_region          = "us-east-1"
  project_name        = "fixture"
  environment         = "test"
  data_bucket_name    = "fixture-source-data"
  backups_bucket_name = "fixture-backups"
  state_bucket_name   = "fixture-state"
  github_oidc         = { owner = "fixture", repository = "platform", environment = "test", owner_id = "123", repository_id = "456", existing_provider_arn = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com" }
}
run "disabled_by_default" {
  command = plan
  assert {
    condition     = length(aws_iam_role.restore) == 0 && length(aws_iam_policy.restore_host_boundary) == 0
    error_message = "Restore must not activate by publication."
  }
}
run "boundary_before_instance" {
  command = plan
  variables { github_restore = { destination_environment = "restore", destination_bucket_name = "fixture-destination" } }
  assert {
    condition     = length(aws_iam_policy.restore_host_boundary) == 1 && length(aws_iam_role.restore) == 0
    error_message = "Exact destination ID required before enabling OIDC."
  }
}
run "scoped_restore" {
  command = plan
  variables { github_restore = { destination_environment = "restore", destination_bucket_name = "fixture-destination", target_instance_id = "i-11111111111111111" } }
  assert {
    condition     = aws_iam_role.restore[0].permissions_boundary == aws_iam_policy.restore_boundary[0].arn && aws_iam_role_policy.restore[0].policy == aws_iam_policy.restore_boundary[0].policy && length(local.restore_policy_json) < 6144
    error_message = "Mandatory boundary and IAM size limit."
  }
  assert {
    condition     = jsondecode(local.restore_policy_json).Statement[1].Resource == "arn:aws:ec2:us-east-1:000000000000:instance/i-11111111111111111" && jsondecode(aws_iam_role.restore[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:workflow"] == "Restore platform (manual)"
    error_message = "Exact SSH target and reviewed workflow."
  }
}
run "reject_source_bucket" {
  command = plan
  variables { github_restore = { destination_environment = "restore", destination_bucket_name = "fixture-source-data" } }
  expect_failures = [var.github_restore]
}
run "reject_source_environment" {
  command = plan
  variables { github_restore = { destination_environment = "test", destination_bucket_name = "fixture-destination" } }
  expect_failures = [var.github_restore]
}
run "reject_wildcard_target" {
  command = plan
  variables { github_restore = { destination_environment = "restore", destination_bucket_name = "fixture-destination", target_instance_id = "*" } }
  expect_failures = [var.github_restore]
}
