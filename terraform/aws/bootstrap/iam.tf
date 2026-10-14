# Provider é compartilhável na conta; não destruir um provider de outro projeto.
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.github_oidc == null ? 0 : (var.github_oidc.existing_provider_arn == null ? 1 : 0)
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  lifecycle {
    prevent_destroy = true
  }
}

locals {
  oidc_provider_arn = var.github_oidc == null ? null : coalesce(var.github_oidc.existing_provider_arn, try(aws_iam_openid_connect_provider.github[0].arn, null))
}

data "aws_iam_policy_document" "github_trust" {
  count = var.github_oidc == null ? 0 : 1
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.github_subject]
    }
  }
}

resource "aws_iam_role" "github" {
  count                = var.github_oidc == null ? 0 : 1
  name                 = "${local.name}-github"
  assume_role_policy   = data.aws_iam_policy_document.github_trust[0].json
  max_session_duration = 7200
  lifecycle {
    prevent_destroy = true
  }
}

# Prepared policy only: no attachment to EC2 or GitHub, no backup implementation.
data "aws_iam_policy_document" "backup_restore" {
  statement {
    actions   = ["s3:GetBucketLocation"]
    resources = [local.backups_bucket_arn]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [local.backups_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["backups/", "backups/*"]
    }
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = ["${local.backups_bucket_arn}/backups/*"]
  }
}
resource "aws_iam_policy" "backup_restore" {
  name   = "${local.name}-backup-restore"
  policy = data.aws_iam_policy_document.backup_restore.json
  lifecycle { prevent_destroy = true }
}
