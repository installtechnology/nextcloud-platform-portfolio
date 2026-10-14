# Região e nomes abaixo são fixtures locais, nunca configuração de cliente.
mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
}

run "reject_shared_data_backup_bucket" {
  command = plan
  variables {
    aws_account_id      = "000000000000"
    backups_bucket_name = "fixture-nextcloud-data-test"
  }
  expect_failures = [var.backups_bucket_name]
}

variables {
  aws_account_id      = "000000000000"
  aws_region          = "us-east-1"
  project_name        = "fixture"
  environment         = "test"
  data_bucket_name    = "fixture-nextcloud-data-test"
  backups_bucket_name = "fixture-nextcloud-backups-test"
}

run "persistent_storage_guards" {
  command = plan
  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.data.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256" && one(one(aws_s3_bucket_server_side_encryption_configuration.backups.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256"
    error_message = "Ambos os buckets exigem SSE-S3."
  }
  assert {
    condition     = aws_s3_bucket_ownership_controls.data.rule[0].object_ownership == "BucketOwnerPreferred" && aws_s3_bucket_ownership_controls.backups.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "Dados precisam aceitar ACL private do driver; backups mantêm ACLs desabilitadas."
  }
  assert {
    condition     = aws_s3_bucket.data.force_destroy == false
    error_message = "Bucket não pode permitir force_destroy."
  }
  assert {
    condition     = aws_s3_bucket_versioning.data.versioning_configuration[0].status == "Enabled"
    error_message = "Versionamento deve permanecer habilitado."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.data.block_public_acls && aws_s3_bucket_public_access_block.data.block_public_policy && aws_s3_bucket_public_access_block.data.ignore_public_acls && aws_s3_bucket_public_access_block.data.restrict_public_buckets
    error_message = "Todos os bloqueios de acesso público devem estar ativos."
  }
  assert {
    condition     = aws_s3_bucket.backups.force_destroy == false
    error_message = "Bucket não pode permitir force_destroy."
  }
  assert {
    condition     = aws_s3_bucket_versioning.backups.versioning_configuration[0].status == "Enabled"
    error_message = "Versionamento deve permanecer habilitado."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.backups.block_public_acls && aws_s3_bucket_public_access_block.backups.block_public_policy && aws_s3_bucket_public_access_block.backups.ignore_public_acls && aws_s3_bucket_public_access_block.backups.restrict_public_buckets
    error_message = "Todos os bloqueios de acesso público devem estar ativos."
  }
}
