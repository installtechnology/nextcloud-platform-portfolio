# Provider simulado; nenhum recurso real ou dependência de runtime/S3 existente.
mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
}
variables {
  aws_account_id      = "000000000000"
  aws_region          = "us-east-1"
  project_name        = "fixture"
  environment         = "test"
  data_bucket_name    = "fixture-nextcloud-data-test"
  state_bucket_name   = "fixture-terraform-state-test"
  backups_bucket_name = "fixture-nextcloud-backups-test"
}
run "oidc_disabled" {
  command = plan
  assert {
    condition     = length(aws_iam_role.github) == 0 && length(aws_iam_openid_connect_provider.github) == 0
    error_message = "OIDC exige configuração explícita."
  }
}
run "oidc_opt_in" {
  command = plan
  variables {
    aws_account_id = "000000000000"
    github_oidc    = { owner = "fixture", repository = "platform", environment = "test" }
  }
  assert {
    condition     = length(aws_iam_role.github) == 1 && length(aws_iam_openid_connect_provider.github) == 1
    error_message = "OIDC explícito deve criar provider e role."
  }
}

run "reuse_oidc_provider" {
  command = plan
  variables {
    aws_account_id = "000000000000"
    github_oidc = {
      owner                 = "fixture"
      repository            = "platform"
      environment           = "test"
      existing_provider_arn = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"
    }
  }
  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 0 && length(aws_iam_role.github) == 1
    error_message = "Provider de cliente deve ser apenas referenciado."
  }
}

run "reject_wildcard_oidc" {
  command = plan
  variables {
    aws_account_id = "000000000000"
    github_oidc    = { owner = "fixture", repository = "*", environment = "test" }
  }
  expect_failures = [var.github_oidc]
}

run "immutable_subject_and_state" {
  command = plan
  variables {
    github_oidc = { owner = "fixture", repository = "platform", environment = "test", owner_id = "123", repository_id = "456" }
  }
  assert {
    condition     = local.github_subject == "repo:fixture@123/platform@456:environment:test"
    error_message = "Subject imutável deve conter os dois IDs, repo e environment exatos."
  }
  assert {
    condition     = !aws_s3_bucket.state.force_destroy && aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled" && one(one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256"
    error_message = "State deve ser preservado, versionado e criptografado."
  }
  assert {
    condition     = local.operational_state_keys == ["states/000000000000/fixture/test/persistent/terraform.tfstate", "states/000000000000/fixture/test/runtime/terraform.tfstate"]
    error_message = "GitHub não administra state bootstrap nem compartilha state entre contas/roots."
  }
}

run "reject_shared_state_bucket" {
  command = plan
  variables { state_bucket_name = "fixture-nextcloud-data-test" }
  expect_failures = [var.state_bucket_name]
}
