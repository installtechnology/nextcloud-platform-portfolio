# Administrative broker must serialize *all* sessions through the coordinator.
# This trust is not a distributed mutex and does not constrain hostile root.
locals {
  authority_actions = [
    "s3:GetBucketPolicy", "s3:PutBucketPolicy", "s3:GetBucketLocation",
    "s3:GetBucketVersioning", "s3:GetLifecycleConfiguration", "s3:GetReplicationConfiguration",
    "s3:GetBucketObjectLockConfiguration", "s3:ListBucketVersions", "s3:ListBucketMultipartUploads"
  ]
  authority_permissions = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "DataPolicyOnly", Effect = "Allow", Action = local.authority_actions, Resource = local.data_bucket_arn,
      Condition = { Bool = { "aws:SecureTransport" = "true" }, StringEquals = { "s3:ResourceAccount" = var.aws_account_id } } },
      { Sid = "Identity", Effect = "Allow", Action = "sts:GetCallerIdentity", Resource = "*" },
      { Sid = "DenyOtherS3", Effect = "Deny", Action = "s3:*", NotResource = local.data_bucket_arn },
      { Sid = "DenyUnlisted", Effect = "Deny", NotAction = concat(local.authority_actions, ["sts:GetCallerIdentity"]), Resource = "*" }
    ]
  })
}

resource "aws_iam_policy" "data_policy_boundary" {
  count  = var.data_policy_authority == null ? 0 : 1
  name   = "${local.name}-data-policy-boundary"
  policy = local.authority_permissions
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role" "data_policy_admin" {
  count                = var.data_policy_authority == null ? 0 : 1
  name                 = "${local.name}-data-policy-admin"
  permissions_boundary = aws_iam_policy.data_policy_boundary[0].arn
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole",
      Principal = { AWS = var.data_policy_authority.issuer_role_arn }
    }]
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policy" "data_policy_admin" {
  count  = var.data_policy_authority == null ? 0 : 1
  name   = "data-policy-coordinator"
  role   = aws_iam_role.data_policy_admin[0].id
  policy = local.authority_permissions
}
