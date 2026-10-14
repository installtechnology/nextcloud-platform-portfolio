data "aws_partition" "current" {}

locals {
  name               = "${var.project_name}-${var.environment}"
  data_bucket_arn    = "arn:${data.aws_partition.current.partition}:s3:::${var.data_bucket_name}"
  state_bucket_arn   = "arn:${data.aws_partition.current.partition}:s3:::${var.state_bucket_name}"
  state_prefix       = "states/${var.aws_account_id}/${var.project_name}/${var.environment}"
  github_subject     = var.github_oidc == null ? null : "repo:${var.github_oidc.owner}${var.github_oidc.owner_id == null ? "" : "@${var.github_oidc.owner_id}"}/${var.github_oidc.repository}${var.github_oidc.repository_id == null ? "" : "@${var.github_oidc.repository_id}"}:environment:${var.github_oidc.environment}"
  backups_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.backups_bucket_name}"
}

# Root administrativo independente. Nenhum recurso/runtime ou terraform_remote_state.
# Backend remoto será habilitado somente depois de seu bucket existir.
