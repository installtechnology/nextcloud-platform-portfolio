mock_provider "aws" {
  override_during = plan
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::000000000000:policy/fixture-test-data-policy-boundary" }
  }
}
variables {
  aws_account_id      = "000000000000"
  aws_region          = "us-east-1"
  project_name        = "fixture"
  environment         = "test"
  data_bucket_name    = "fixture-data"
  backups_bucket_name = "fixture-backups"
  state_bucket_name   = "fixture-state"
}
run "authority_disabled" {
  command = plan
  assert {
    condition     = length(aws_iam_role.data_policy_admin) == 0 && length(aws_iam_policy.data_policy_boundary) == 0 && length(aws_iam_role_policy.data_policy_admin) == 0
    error_message = "Authority must remain opt-in."
  }
}
run "dedicated_broker" {
  command = plan
  variables {
    data_policy_authority = { issuer_role_arn = "arn:aws:iam::000000000000:role/administrative-broker" }
  }
  assert {
    condition     = aws_iam_role.data_policy_admin[0].permissions_boundary == aws_iam_policy.data_policy_boundary[0].arn
    error_message = "Authority must have its narrow boundary."
  }
  assert {
    condition     = !contains(local.authority_actions, "s3:DeleteBucketPolicy") && !contains(local.authority_actions, "s3:PutObject")
    error_message = "Authority cannot delete policy or write objects."
  }
}
run "reject_deploy_issuer" {
  command = plan
  variables {
    data_policy_authority = { issuer_role_arn = "arn:aws:iam::000000000000:role/fixture-test-github" }
  }
  expect_failures = [var.data_policy_authority]
}
