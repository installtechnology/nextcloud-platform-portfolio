# No privilege changes to backup/application/normal deploy. Dedicated opt-in
# workflow identity; administrative grants are on a separate state prefix.
locals {
  creation_prefix   = "control/reprovision/${var.aws_account_id}/${var.project_name}/${var.environment}"
  creation_role_arn = "arn:aws:iam::${var.aws_account_id}:role/${local.name}-reprovision"
  creation_name     = "${var.project_name}-${try(var.github_reprovision.destination_environment, "unselected")}"
  creation_bucket   = "arn:aws:s3:::${try(var.github_reprovision.destination_data_bucket, "unselected")}"
  creation_state    = "states/${var.aws_account_id}/${var.project_name}/${try(var.github_reprovision.destination_environment, "unselected")}/restore/terraform.tfstate"
  creation_removed_sids = [
    "ListOperationalState", "OperationalStateOnly", "OperationalLocksOnly",
    "ManageTwoApplicationBucketsConfiguration", "ManageBackupBucketPolicyOnly", "DenyDirectDataPolicyMutation"
  ]
  # Reuse the already audited EC2 multi-resource authorization, changing only
  # the destination scope. Removal is by exact action, never ec2:* / admin IAM.
  creation_filtered_actions = [
    "ec2:DeleteVpc", "ec2:DeleteSubnet", "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway",
    "ec2:DeleteRoute", "ec2:DisassociateRouteTable", "ec2:DeleteRouteTable", "ec2:DeleteSecurityGroup",
    "ec2:DeleteKeyPair", "ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances", "ec2:DeleteTags",
    "iam:DeleteRole", "iam:UpdateAssumeRolePolicy", "iam:UpdateRole", "iam:UntagRole", "iam:DeleteRolePolicy",
    "iam:DeleteInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:UntagInstanceProfile"
  ]
  creation_base = [for s in try(jsondecode(data.aws_iam_policy_document.operational.json).Statement, []) :
    merge(jsondecode(replace(replace(replace(jsonencode(s), local.name, local.creation_name),
      local.data_bucket_arn, local.creation_bucket), jsonencode(var.environment),
      jsonencode(try(var.github_reprovision.destination_environment, "unselected")))), {
      Action = [for a in(try(tolist(s.Action), [tostring(s.Action)])) : a if !contains(local.creation_filtered_actions, a)]
    }) if !contains(local.creation_removed_sids, try(s.Sid, ""))
  ]
  creation_statements = concat(local.creation_base, [
    { Sid = "SourcePolicyReadOnly", Effect = "Allow", Action = "s3:GetBucketPolicy", Resource = local.data_bucket_arn },
    { Sid = "ControlBucketMetadata", Effect = "Allow", Action = ["s3:GetBucketLocation", "s3:GetBucketPolicy", "s3:GetBucketVersioning"], Resource = local.state_bucket_arn },
    { Sid = "CreationHistory", Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = local.state_bucket_arn,
    Condition = { StringLike = { "s3:prefix" = ["${local.creation_prefix}/lease.json", "${local.creation_prefix}/grants/*", local.creation_state] } } },
    { Sid = "ReadGrantsAndJournal", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"],
    Resource = ["${local.state_bucket_arn}/${local.creation_prefix}/lease.json", "${local.state_bucket_arn}/${local.creation_prefix}/grants/*"] },
    { Sid = "ConditionalCreationJournal", Effect = "Allow", Action = "s3:PutObject", Resource = "${local.state_bucket_arn}/${local.creation_prefix}/lease.json",
    Condition = { StringEquals = { "s3:x-amz-server-side-encryption" = "AES256" } } },
    { Sid = "CreationState", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${local.state_bucket_arn}/${local.creation_state}" },
    { Sid = "CreationNativeTerraformLock", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${local.state_bucket_arn}/${local.creation_state}.tflock" },
    { Sid = "NewDestinationConfiguration", Effect = "Allow", Action = [
      "s3:CreateBucket", "s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketAcl",
      "s3:GetBucketPolicy", "s3:PutBucketPolicy", "s3:GetBucketPublicAccessBlock", "s3:PutBucketPublicAccessBlock",
      "s3:GetBucketOwnershipControls", "s3:PutBucketOwnershipControls", "s3:GetEncryptionConfiguration", "s3:PutEncryptionConfiguration",
      "s3:GetBucketVersioning", "s3:PutBucketVersioning", "s3:GetBucketTagging", "s3:PutBucketTagging",
      "s3:GetLifecycleConfiguration", "s3:GetBucketLogging", "s3:GetBucketWebsite", "s3:GetReplicationConfiguration",
      "s3:GetBucketObjectLockConfiguration", "s3:GetAccelerateConfiguration", "s3:GetBucketRequestPayment", "s3:GetBucketCORS"
    ], Resource = local.creation_bucket },
    { Sid = "SSMOnlineReadOnly", Effect = "Allow", Action = "ssm:DescribeInstanceInformation", Resource = "*",
    Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } } },
    { Sid = "DenyGrantWrites", Effect = "Deny", Action = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "${local.state_bucket_arn}/${local.creation_prefix}/grants/*" },
    { Sid = "DenyControlDeletion", Effect = "Deny", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "${local.state_bucket_arn}/${local.creation_prefix}/*" },
    { Sid = "DenySourceAndBackupMutation", Effect = "Deny", NotAction = ["s3:GetBucketPolicy", "s3:GetBucketLocation"],
    Resource = [local.data_bucket_arn, "${local.data_bucket_arn}/*", local.backups_bucket_arn, "${local.backups_bucket_arn}/*"] },
    { Sid = "DenyAdministrativeDestruction", Effect = "Deny", Action = ["ec2:TerminateInstances", "s3:DeleteBucket", "s3:DeleteBucketPolicy", "iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:AttachRolePolicy"], Resource = "*" }
  ])
  # Sids are diagnostic labels, not authorization controls. Omit them in the
  # two compiled documents to fit real account/bucket names without changing
  # a single action, resource, condition or deny. Bucket-policy Sids stay intact.
  creation_policy = jsonencode({ Version = "2012-10-17", Statement = [
    for s in local.creation_statements : { for k, v in s : k => v if k != "Sid" }
  ] })
  # Managed boundaries have a 6 KiB limit. Preserve IAM conditions verbatim;
  # consolidate regional EC2 APIs and S3 while the inline policy keeps exact
  # resource/tag/creation conditions. Effective permissions are the intersection.
  # Explicit Denies prevent the union from granting state admin,
  # source writes, grant writes or object deletion outside the native tf lock.
  creation_boundary_statements = concat(
    [for s in local.creation_base : s if !anytrue([for a in s.Action : startswith(a, "s3:") || startswith(a, "ec2:")])],
    [{ Sid = "RegionalCreationEC2", Effect = "Allow", Action = distinct(flatten([
      for s in local.creation_base : s.Action if anytrue([for a in s.Action : startswith(a, "ec2:")])
      ])), Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } } },
      { Sid = "ScopedCreationStorage", Effect = "Allow", Action = distinct(flatten([
        for s in local.creation_statements : try(tolist(s.Action), [tostring(s.Action)])
        if s.Effect == "Allow" && anytrue([for a in try(tolist(s.Action), [tostring(s.Action)]) : startswith(a, "s3:")])
        ])), Resource = [local.creation_bucket, local.state_bucket_arn, local.data_bucket_arn,
      "${local.state_bucket_arn}/${local.creation_prefix}/*", "${local.state_bucket_arn}/${local.creation_state}", "${local.state_bucket_arn}/${local.creation_state}.tflock"] },
      { Sid = "SSMOnline", Effect = "Allow", Action = "ssm:DescribeInstanceInformation", Resource = "*",
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } } },
      { Sid = "Identity", Effect = "Allow", Action = "sts:GetCallerIdentity", Resource = "*" },
      { Sid = "NoStateAdministration", Effect = "Deny", NotAction = ["s3:GetBucketLocation", "s3:GetBucketPolicy", "s3:GetBucketVersioning", "s3:ListBucket", "s3:ListBucketVersions"], Resource = local.state_bucket_arn },
      { Sid = "NoObjectDeletionExceptNativeLock", Effect = "Deny", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], NotResource = "${local.state_bucket_arn}/${local.creation_state}.tflock" }
    ], [for s in local.creation_statements : s if s.Effect == "Deny"]
  )
  creation_boundary = jsonencode({ Version = "2012-10-17", Statement = [
    for s in local.creation_boundary_statements : { for k, v in s : k => v if k != "Sid" }
  ] })
}

resource "aws_iam_policy" "reprovision_boundary" {
  count  = var.github_reprovision == null ? 0 : 1
  name   = "${local.name}-reprovision-boundary"
  policy = local.creation_boundary
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = length(local.creation_base) > 0 && length(local.creation_boundary) <= 6144 && length(local.creation_policy) <= 10240
      error_message = "Creation policy exceeds IAM size limits; do not publish/apply an unusable boundary."
    }
  }
}
resource "aws_iam_role" "reprovision" {
  count                = var.github_reprovision == null ? 0 : 1
  name                 = "${local.name}-reprovision"
  permissions_boundary = aws_iam_policy.reprovision_boundary[0].arn
  max_session_duration = 7200
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc_provider_arn }
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud"                 = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub"                 = local.github_subject
      "token.actions.githubusercontent.com:ref"                 = "refs/heads/main"
      "token.actions.githubusercontent.com:workflow"            = "Deploy platform (manual)"
      "token.actions.githubusercontent.com:repository_id"       = var.github_oidc.repository_id
      "token.actions.githubusercontent.com:repository_owner_id" = var.github_oidc.owner_id
    } }
  }] })
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role_policy" "reprovision" {
  count  = var.github_reprovision == null ? 0 : 1
  name   = "grant-bound-recovery-creation"
  role   = aws_iam_role.reprovision[0].id
  policy = local.creation_policy
}
resource "aws_iam_role_policy" "reprovision_issuer" {
  count = var.github_reprovision == null ? 0 : 1
  name  = "exclusive-recovery-creation-grants"
  role  = split("/", var.github_reprovision.grant_issuer_arn)[length(split("/", var.github_reprovision.grant_issuer_arn)) - 1]
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetBucketPolicy", "s3:GetBucketVersioning"], Resource = local.state_bucket_arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], Resource = ["${local.state_bucket_arn}/${local.creation_prefix}/grants/*", "${local.state_bucket_arn}/${local.creation_prefix}/lease.json"] },
    { Effect = "Allow", Action = "s3:ListBucketVersions", Resource = local.state_bucket_arn,
    Condition = { StringLike = { "s3:prefix" = ["${local.creation_prefix}/grants/*", "${local.creation_prefix}/lease.json"] } } },
    { Effect = "Deny", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "${local.state_bucket_arn}/${local.creation_prefix}/*" }
  ] })
}
output "reprovision_role_arn" { value = try(aws_iam_role.reprovision[0].arn, null) }
