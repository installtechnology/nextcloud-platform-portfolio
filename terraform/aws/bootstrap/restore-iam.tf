# Preparatory only. No changes when github_restore is null.
locals {
  restore_enabled    = var.github_restore != null && (try(var.github_restore.target_instance_id, null) != null || var.github_reprovision != null)
  restore_target_arn = "arn:aws:ec2:${var.aws_region}:${var.aws_account_id}:instance/${var.github_reprovision == null ? coalesce(try(var.github_restore.target_instance_id, null), "unselected") : "*"}"
  restore_session_conditions = var.github_reprovision == null ? { StringEquals = {
    "ssm:resourceTag/aws:ssmmessages:target-id"  = coalesce(try(var.github_restore.target_instance_id, null), "unselected"),
    "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}"
    } } : {
    StringEquals = { "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}" }
    StringLike   = { "ssm:resourceTag/aws:ssmmessages:target-id" = "i-*" }
  }
  restore_destination_arn = "arn:aws:s3:::${try(var.github_restore.destination_bucket_name, "unselected")}"
  restore_storage_conditions = {
    Bool         = { "aws:SecureTransport" = "true" }
    StringEquals = { "s3:ResourceAccount" = var.aws_account_id }
  }
  restore_actions = concat(["sts:GetCallerIdentity", "ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ssm:DescribeInstanceInformation", "ssm:StartSession", "ssm:TerminateSession", "s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketVersions", "s3:GetObjectVersion", "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration", "s3:GetBucketPublicAccessBlock", "s3:GetBucketOwnershipControls", "s3:GetBucketPolicy", "s3:PutObject", "s3:PutObjectAcl"], var.github_reprovision == null ? [] : ["ec2:GetConsoleOutput"])
  restore_policy_json = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Sid = "RegionalDiscovery", Effect = "Allow", Action = ["ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ssm:DescribeInstanceInformation"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } } },
    { Sid = "ExactSSHNode", Effect = "Allow", Action = "ssm:StartSession", Resource = local.restore_target_arn, Condition = {
      StringEquals = { "ssm:resourceTag/Project" = var.project_name, "ssm:resourceTag/Environment" = try(var.github_restore.destination_environment, "unselected") }
      BoolIfExists = { "ssm:SessionDocumentAccessCheck" = "true" }
    } },
    { Sid = "SSHDocumentOnly", Effect = "Allow", Action = "ssm:StartSession", Resource = "arn:aws:ssm:${var.aws_region}::document/AWS-StartSSHSession" },
    { Sid = "OwnFederatedSession", Effect = "Allow", Action = "ssm:TerminateSession", Resource = "arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:session/*", Condition = local.restore_session_conditions },
    { Sid = "StorageLocations", Effect = "Allow", Action = "s3:GetBucketLocation", Resource = [local.data_bucket_arn, local.backups_bucket_arn, local.restore_destination_arn], Condition = local.restore_storage_conditions },
    { Sid = "SourceInventory", Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = local.data_bucket_arn, Condition = local.restore_storage_conditions },
    { Sid = "BackupPrefix", Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = local.backups_bucket_arn, Condition = merge(local.restore_storage_conditions, { StringLike = { "s3:prefix" = ["backups/", "backups/*"] } }) },
    { Sid = "ReadExactVersions", Effect = "Allow", Action = "s3:GetObjectVersion", Resource = ["${local.data_bucket_arn}/*", "${local.backups_bucket_arn}/backups/*", "${local.restore_destination_arn}/*"], Condition = local.restore_storage_conditions },
    { Sid = "VerifyDestination", Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration", "s3:GetBucketPublicAccessBlock", "s3:GetBucketOwnershipControls", "s3:GetBucketPolicy"], Resource = local.restore_destination_arn, Condition = local.restore_storage_conditions },
    { Sid = "CopyPrivateAES256", Effect = "Allow", Action = "s3:PutObject", Resource = "${local.restore_destination_arn}/*", Condition = merge(local.restore_storage_conditions, { StringEquals = { "s3:ResourceAccount" = var.aws_account_id, "s3:x-amz-server-side-encryption" = "AES256", "s3:x-amz-acl" = "private" } }) },
    { Sid = "PrivateAclOnly", Effect = "Allow", Action = "s3:PutObjectAcl", Resource = "${local.restore_destination_arn}/*", Condition = merge(local.restore_storage_conditions, { StringEquals = { "s3:ResourceAccount" = var.aws_account_id, "s3:x-amz-acl" = "private" } }) },
    { Sid = "DenyOtherAPIs", Effect = "Deny", NotAction = local.restore_actions, Resource = "*" },
    { Sid = "DenyStorageOutsideScope", Effect = "Deny", Action = "s3:*", NotResource = [local.data_bucket_arn, "${local.data_bucket_arn}/*", local.backups_bucket_arn, "${local.backups_bucket_arn}/backups/*", local.restore_destination_arn, "${local.restore_destination_arn}/*"] },
    { Sid = "DenyOtherSessionTargets", Effect = "Deny", Action = "ssm:StartSession", NotResource = [local.restore_target_arn, "arn:aws:ssm:${var.aws_region}::document/AWS-StartSSHSession"] },
    { Sid = "DenySourceMutations", Effect = "Deny", NotAction = ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketVersions", "s3:GetObjectVersion"], Resource = [local.data_bucket_arn, "${local.data_bucket_arn}/*", local.backups_bucket_arn, "${local.backups_bucket_arn}/*"] },
    { Sid = "DenyState", Effect = "Deny", Action = "s3:*", Resource = [local.state_bucket_arn, "${local.state_bucket_arn}/*"] }
    ], var.github_reprovision == null ? [] : [{ Sid = "VerifyPreparedHostKey", Effect = "Allow", Action = "ec2:GetConsoleOutput",
      Resource = local.restore_target_arn, Condition = { StringEquals = { "aws:ResourceTag/Project" = var.project_name,
      "aws:ResourceTag/Environment" = var.github_restore.destination_environment, "aws:RequestedRegion" = var.aws_region } }
  }]) })
  restore_host_boundary_json = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "SSMAgent", Effect = "Allow", Action = local.recovery_agent_actions, Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } } },
    { Sid = "DestinationLocation", Effect = "Allow", Action = ["s3:GetBucketLocation", "s3:ListBucket"], Resource = local.restore_destination_arn },
    { Sid = "NextcloudStorage", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], Resource = "${local.restore_destination_arn}/*" },
    { Sid = "PrivateAcl", Effect = "Allow", Action = "s3:PutObjectAcl", Resource = "${local.restore_destination_arn}/*", Condition = { StringEquals = { "s3:x-amz-acl" = "private" } } },
    { Sid = "DenyOtherStorage", Effect = "Deny", Action = "s3:*", NotResource = [local.restore_destination_arn, "${local.restore_destination_arn}/*"] }
  ] })
}
resource "aws_iam_policy" "restore_host_boundary" {
  count  = var.github_restore == null ? 0 : 1
  name   = "${var.project_name}-${var.github_restore.destination_environment}-ec2-boundary"
  policy = local.restore_host_boundary_json
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_policy" "restore_boundary" {
  count  = local.restore_enabled ? 1 : 0
  name   = "${local.name}-restore-boundary"
  policy = local.restore_policy_json
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role" "restore" {
  count                = local.restore_enabled ? 1 : 0
  name                 = "${local.name}-restore"
  permissions_boundary = aws_iam_policy.restore_boundary[0].arn
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc_provider_arn }
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud"                 = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub"                 = local.github_subject
      "token.actions.githubusercontent.com:ref"                 = "refs/heads/main"
      "token.actions.githubusercontent.com:workflow"            = "Restore platform (manual)"
      "token.actions.githubusercontent.com:repository_id"       = var.github_oidc.repository_id
      "token.actions.githubusercontent.com:repository_owner_id" = var.github_oidc.owner_id
    } }
  }] })
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role_policy" "restore" {
  count  = local.restore_enabled ? 1 : 0
  name   = "independent-restore-only"
  role   = aws_iam_role.restore[0].id
  policy = local.restore_policy_json
}
output "restore_host_boundary_arn" { value = try(aws_iam_policy.restore_host_boundary[0].arn, null) }
output "restore_role_arn" { value = try(aws_iam_role.restore[0].arn, null) }
