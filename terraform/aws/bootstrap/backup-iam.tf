# Opt-in administrativo. Não altera deploy/EC2, não executa backup nem abre SSH.
locals {
  backup_enabled = var.github_backup != null && var.github_oidc != null
  # SSH requests can omit this service context key. BoolIfExists still rejects
  # false and enforces the separately scoped SSH document permission check.
  # Keep the deploy guard unchanged: this correction is backup-role-only.
  backup_session_statements = concat([for statement in local.recovery_session_statements :
    merge(statement, {
      Condition = {
        StringEquals = { "ssm:resourceTag/Project" = var.project_name, "ssm:resourceTag/Environment" = var.environment }
        BoolIfExists = { "ssm:SessionDocumentAccessCheck" = "true" }
      }
    }) if statement.Sid == "SSHToOneTaggedTarget"
    ], var.recovery_ssm != null ? [
    # AWS system tags carry the federated principal ID, not the session ARN name.
    # Both owner and target must match; missing tags never grant termination.
    {
      Sid      = "OwnSessionsOnly", Effect = "Allow", Action = "ssm:TerminateSession"
      Resource = "arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:session/*"
      Condition = { StringEquals = {
        "ssm:resourceTag/aws:ssmmessages:target-id"  = try(var.recovery_ssm.instance_id, "unselected")
        "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}"
      } }
    }
  ] : [], [for statement in local.recovery_session_statements : statement if !contains(["SSHToOneTaggedTarget", "OwnSessionsOnly"], statement.Sid)])
  # Pinned plugin 1.2.707.0 opens its client websocket using the StartSession
  # token, without SigV4 credentials. Do not invent unsupported session-tag
  # conditions for ssmmessages or grant a wildcard channel permission.
  backup_api_actions = concat([
    "ec2:DescribeInstances", "ec2:DescribeInstanceStatus",
    "s3:GetBucketLocation", "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration",
    "s3:ListBucket", "s3:ListBucketVersions", "s3:GetObject", "s3:GetObjectVersion",
    "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"
  ], var.recovery_ssm == null ? [] : [for action in local.recovery_session_actions : action if action != "ssmmessages:OpenDataChannel"])
  backup_s3_conditions = {
    Bool         = { "aws:SecureTransport" = "true" }
    StringEquals = { "s3:ResourceAccount" = var.aws_account_id }
  }
  # JSON calculado pelo Terraform permite testar a policy real mesmo com provider mock.
  backup_policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid       = "RegionalDiscoveryOnly", Effect = "Allow"
        Action    = ["ec2:DescribeInstances", "ec2:DescribeInstanceStatus"], Resource = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid       = "VerifyStorageMetadata", Effect = "Allow"
        Action    = ["s3:GetBucketLocation", "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration"]
        Resource  = [local.data_bucket_arn, local.backups_bucket_arn]
        Condition = local.backup_s3_conditions
      },
      {
        Sid       = "InventoryDataVersions", Effect = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = local.data_bucket_arn
        Condition = local.backup_s3_conditions
      },
      {
        Sid = "CheckDataObjectVersions", Effect = "Allow"
        # HEAD usa GetObject/GetObjectVersion: IAM não oferece HEAD sem GET de conteúdo.
        Action    = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${local.data_bucket_arn}/*"
        Condition = local.backup_s3_conditions
      },
      {
        Sid    = "ListBackupPrefix", Effect = "Allow"
        Action = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = local.backups_bucket_arn
        Condition = merge(local.backup_s3_conditions, {
          StringLike = { "s3:prefix" = ["backups/", "backups/*"] }
        })
      },
      {
        Sid       = "ReadAndVerifyBackupArtifacts", Effect = "Allow"
        Action    = ["s3:GetObject", "s3:GetObjectVersion", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource  = "${local.backups_bucket_arn}/backups/*"
        Condition = local.backup_s3_conditions
      },
      {
        Sid    = "WriteBackupArtifacts", Effect = "Allow"
        Action = "s3:PutObject", Resource = "${local.backups_bucket_arn}/backups/*"
        Condition = merge(local.backup_s3_conditions, {
          StringEqualsIfExists = { "s3:x-amz-server-side-encryption" = "AES256" }
          Null                 = { "s3:x-amz-server-side-encryption-customer-algorithm" = "true" }
        })
      },
      {
        Sid      = "DenyStateAccess", Effect = "Deny", Action = "s3:*"
        Resource = [local.state_bucket_arn, "${local.state_bucket_arn}/*"]
      },
      {
        Sid         = "DenyS3OutsideScope", Effect = "Deny", Action = "s3:*"
        NotResource = [local.data_bucket_arn, "${local.data_bucket_arn}/*", local.backups_bucket_arn, "${local.backups_bucket_arn}/backups/*"]
      },
      {
        Sid       = "DenyDataMutations", Effect = "Deny"
        NotAction = ["s3:GetBucketLocation", "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration", "s3:ListBucket", "s3:ListBucketVersions", "s3:GetObject", "s3:GetObjectVersion"]
        Resource  = [local.data_bucket_arn, "${local.data_bucket_arn}/*"]
      },
      {
        Sid      = "DenyObjectDeletion", Effect = "Deny"
        Action   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource = ["${local.data_bucket_arn}/*", "${local.backups_bucket_arn}/*"]
      },
      {
        Sid       = "DenyListingOutsideBackupPrefix", Effect = "Deny"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = local.backups_bucket_arn
        Condition = { StringNotLike = { "s3:prefix" = ["backups/", "backups/*"] } }
      },
      {
        Sid       = "DenyInsecureS3", Effect = "Deny", Action = "s3:*", Resource = "*"
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "DenyUnlistedAPIs", Effect = "Deny"
        NotAction = local.backup_api_actions, Resource = "*"
      }
    ], local.backup_session_statements)
  })
  backup_trust_json = local.backup_enabled ? jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.oidc_provider_arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud"                 = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub"                 = local.github_subject
        "token.actions.githubusercontent.com:ref"                 = "refs/heads/${var.github_backup.branch}"
        "token.actions.githubusercontent.com:workflow"            = var.github_backup.workflow_name
        "token.actions.githubusercontent.com:repository_id"       = var.github_oidc.repository_id
        "token.actions.githubusercontent.com:repository_owner_id" = var.github_oidc.owner_id
      } }
    }]
  }) : null
}

resource "aws_iam_policy" "backup_boundary" {
  count  = local.backup_enabled ? 1 : 0
  name   = "${local.name}-backup-boundary"
  policy = local.backup_policy_json
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role" "backup" {
  count                = local.backup_enabled ? 1 : 0
  name                 = "${local.name}-backup"
  assume_role_policy   = local.backup_trust_json
  permissions_boundary = aws_iam_policy.backup_boundary[0].arn
  max_session_duration = 3600
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policy" "backup" {
  count  = local.backup_enabled ? 1 : 0
  name   = "manual-backup-only"
  role   = aws_iam_role.backup[0].id
  policy = local.backup_policy_json
}
