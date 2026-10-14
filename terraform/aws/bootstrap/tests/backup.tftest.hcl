# Somente plan com provider mock; nenhuma chamada AWS/backend remoto.
mock_provider "aws" {
  override_during = plan
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::000000000000:policy/fixture-test-backup-boundary" }
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
  github_oidc = {
    owner                 = "fixture", repository = "platform", environment = "test"
    owner_id              = "123", repository_id = "456"
    existing_provider_arn = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"
  }
}
run "backup_disabled_by_default" {
  command = plan
  assert {
    condition     = length(aws_iam_role.backup) == 0 && length(aws_iam_role_policy.backup) == 0 && length(aws_iam_policy.backup_boundary) == 0
    error_message = "PR4 não pode ativar backup sem opt-in administrativo."
  }
  assert {
    condition     = output.backup_role_arn == null && output.backup_permissions_boundary_arn == null && output.backup_oidc_requirements == null
    error_message = "Opt-in desabilitado não deve publicar identidade de backup."
  }
}
run "legacy_subject_backup_disabled" {
  command = plan
  variables { github_oidc = { owner = "fixture", repository = "platform", environment = "test" } }
  assert {
    condition     = local.github_subject == "repo:fixture/platform:environment:test" && length(aws_iam_role.backup) == 0 && output.backup_oidc_requirements == null
    error_message = "Deploy legado permanece compatível sem habilitar backup."
  }
}
run "backup_security_contract" {
  command = plan
  variables { github_backup = { branch = "main", workflow_name = "Backup platform (manual)" } }
  assert {
    condition     = length(aws_iam_role.backup) == 1 && length(aws_iam_role_policy.backup) == 1 && length(aws_iam_policy.backup_boundary) == 1 && length(aws_iam_openid_connect_provider.github) == 0
    error_message = "Backup exige role/policy/boundary próprias e reutiliza OIDC existente."
  }
  assert {
    condition     = aws_iam_role.backup[0].permissions_boundary == aws_iam_policy.backup_boundary[0].arn && aws_iam_role.backup[0].max_session_duration == 3600 && aws_iam_role_policy.backup[0].policy == aws_iam_policy.backup_boundary[0].policy
    error_message = "Role deve manter ceiling igual à policy e sessão de no máximo uma hora."
  }
  assert {
    condition = jsondecode(aws_iam_role.backup[0].assume_role_policy).Statement[0].Condition.StringEquals == {
      "token.actions.githubusercontent.com:aud"                 = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub"                 = "repo:fixture@123/platform@456:environment:test"
      "token.actions.githubusercontent.com:ref"                 = "refs/heads/main"
      "token.actions.githubusercontent.com:workflow"            = "Backup platform (manual)"
      "token.actions.githubusercontent.com:repository_id"       = "456"
      "token.actions.githubusercontent.com:repository_owner_id" = "123"
    }
    error_message = "Claims exatas devem ficar separadas; subject Environment não contém branch."
  }
  assert {
    condition     = length(local.backup_policy_json) < 6144 && aws_iam_role.backup[0].name == "fixture-test-backup"
    error_message = "Policy boundary deve caber no limite IAM e role deve ser distinta de deploy/EC2."
  }
}
run "reject_backup_without_oidc" {
  command = plan
  variables {
    github_oidc   = null
    github_backup = { branch = "main", workflow_name = "Backup platform (manual)" }
  }
  expect_failures = [var.github_backup]
}
run "reject_backup_without_immutable_ids" {
  command = plan
  variables {
    github_oidc   = { owner = "fixture", repository = "platform", environment = "test" }
    github_backup = { branch = "main", workflow_name = "Backup platform (manual)" }
  }
  expect_failures = [var.github_backup]
}
run "reject_wildcard_branch" {
  command = plan
  variables { github_backup = { branch = "*", workflow_name = "Backup platform (manual)" } }
  expect_failures = [var.github_backup]
}
run "reject_invalid_branch" {
  command = plan
  variables { github_backup = { branch = "main/../release", workflow_name = "Backup platform (manual)" } }
  expect_failures = [var.github_backup]
}
run "reject_hidden_branch_component" {
  command = plan
  variables { github_backup = { branch = "main/.hidden", workflow_name = "Backup platform (manual)" } }
  expect_failures = [var.github_backup]
}
run "reject_foreign_oidc_provider" {
  command = plan
  variables {
    github_oidc = {
      owner                 = "fixture", repository = "platform", environment = "test", owner_id = "123", repository_id = "456"
      existing_provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
    }
    github_backup = { branch = "main", workflow_name = "Backup platform (manual)" }
  }
  expect_failures = [var.github_oidc]
}
run "reject_wildcard_workflow" {
  command = plan
  variables { github_backup = { branch = "main", workflow_name = "Backup *" } }
  expect_failures = [var.github_backup]
}
run "reject_wildcard_backups_bucket" {
  command = plan
  variables {
    backups_bucket_name = "*"
    github_backup       = { branch = "main", workflow_name = "Backup platform (manual)" }
  }
  expect_failures = [var.github_backup]
}
run "reject_wildcard_data_bucket" {
  command = plan
  variables {
    data_bucket_name = "*"
    github_backup    = { branch = "main", workflow_name = "Backup platform (manual)" }
  }
  expect_failures = [var.github_backup]
}
run "maximum_names_policy_size" {
  command = plan
  variables {
    project_name        = "ppppppppppppppppppppppppp"
    environment         = "eeeeeeeeeeeeeee"
    data_bucket_name    = "ddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    backups_bucket_name = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    state_bucket_name   = "sssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssss"
    github_oidc = {
      owner                 = "fixture", repository = "platform", environment = "eeeeeeeeeeeeeee"
      owner_id              = "123", repository_id = "456"
      existing_provider_arn = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"
    }
    github_backup = { branch = "main", workflow_name = "Backup platform (manual)" }
  }
  assert {
    condition     = length(local.backup_policy_json) < 6144
    error_message = "Policy deve caber no limite managed de 6144 caracteres mesmo com bucket máximo."
  }
}
