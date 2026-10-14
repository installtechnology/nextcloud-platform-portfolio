mock_provider "aws" {
  override_during = plan
  mock_data "aws_partition" { defaults = { partition = "aws", dns_suffix = "amazonaws.com" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{}" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::000000000000:policy/synthetic-boundary" } }
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
run "recovery_off_by_default" {
  command = plan
  assert {
    condition     = length(aws_iam_role.deploy_guard) == 0 && length(aws_iam_role_policy.recovery_agent) == 0
    error_message = "Publishing code cannot enable guard or agent permissions."
  }
  assert {
    condition     = !contains(local.backup_api_actions, "ssm:StartSession")
    error_message = "PR4 default backup policy must remain without SSM."
  }
}
run "explicit_recovery_scope" {
  command = plan
  variables {
    github_oidc   = { owner = "fixture", repository = "platform", environment = "test", owner_id = "1234", repository_id = "5678", existing_provider_arn = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com" }
    github_backup = { branch = "main", workflow_name = "Backup platform (operational manual)" }
    recovery_ssm  = { instance_id = "i-aaaaaaaaaaaaaaaaa" }
  }
  assert {
    condition     = aws_iam_role.deploy_guard[0].permissions_boundary == aws_iam_policy.deploy_guard_boundary[0].arn
    error_message = "Dedicated guard boundary is mandatory."
  }
  assert {
    condition     = jsondecode(aws_iam_role.deploy_guard[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:workflow"] == "Deploy platform (manual)"
    error_message = "Deploy guard must restrict its exact workflow identity."
  }
  assert {
    condition     = !contains(local.backup_api_actions, "s3:PutBucketPolicy") && !contains(local.backup_api_actions, "ec2:AuthorizeSecurityGroupIngress") && !contains(local.recovery_session_actions, "ssm:SendCommand")
    error_message = "Backup cannot administer policy, open SG or RunCommand."
  }
  assert {
    condition     = local.recovery_session_statements[0].Resource == "arn:aws:ec2:us-east-1:000000000000:instance/i-aaaaaaaaaaaaaaaaa" && local.recovery_session_statements[1].Resource == "arn:aws:ssm:us-east-1::document/AWS-StartSSHSession"
    error_message = "SSM requires exact target and SSH document."
  }
  assert {
    condition     = local.backup_session_statements[0].Condition.BoolIfExists["ssm:SessionDocumentAccessCheck"] == "true" && local.recovery_session_statements[0].Condition.Bool["ssm:SessionDocumentAccessCheck"] == "true"
    error_message = "Correct only the backup context-key condition; deploy guard must remain unchanged."
  }
  assert {
    condition     = local.backup_session_statements[1].Action == "ssm:TerminateSession" && local.backup_session_statements[1].Resource == "arn:aws:ssm:us-east-1:000000000000:session/*" && local.backup_session_statements[1].Condition.StringEquals == { "ssm:resourceTag/aws:ssmmessages:target-id" = "i-aaaaaaaaaaaaaaaaa", "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}" } && !contains(local.backup_api_actions, "ssmmessages:OpenDataChannel")
    error_message = "Federated termination needs exact AWS owner and target tags; client channels use the scoped token."
  }
  assert {
    condition     = local.backup_session_statements[0].Resource == local.recovery_session_statements[0].Resource && local.backup_session_statements[0].Condition.StringEquals == local.recovery_session_statements[0].Condition.StringEquals && local.backup_session_statements[2] == local.recovery_session_statements[1] && local.backup_session_statements[3] == local.recovery_session_statements[3]
    error_message = "Preserve exact instance, required tags, SSH document and own-session permissions."
  }
}
run "missing_backup_identity_rejected" {
  command = plan
  variables { recovery_ssm = { instance_id = "i-aaaaaaaaaaaaaaaaa" } }
  expect_failures = [var.recovery_ssm]
}
