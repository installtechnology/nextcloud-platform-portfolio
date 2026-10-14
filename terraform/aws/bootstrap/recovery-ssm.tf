# No SG writes, RunCommand, SSH shell document or deploy-role reuse.
locals {
  recovery_session_actions = ["ssm:StartSession", "ssm:TerminateSession", "ssm:DescribeInstanceInformation", "ssmmessages:OpenDataChannel"]
  recovery_session_statements = [for statement in [
    {
      Sid      = "SSHToOneTaggedTarget", Effect = "Allow", Action = "ssm:StartSession"
      Resource = "arn:aws:ec2:${var.aws_region}:${var.aws_account_id}:instance/${try(var.recovery_ssm.instance_id, "unselected")}"
      Condition = {
        StringEquals = { "ssm:resourceTag/Project" = var.project_name, "ssm:resourceTag/Environment" = var.environment }
        Bool         = { "ssm:SessionDocumentAccessCheck" = "true" }
      }
    },
    {
      Sid      = "SSHDocumentOnly", Effect = "Allow", Action = "ssm:StartSession"
      Resource = "arn:aws:ssm:${var.aws_region}::document/AWS-StartSSHSession"
    },
    {
      Sid      = "OwnSessionsOnly", Effect = "Allow", Action = ["ssm:TerminateSession", "ssmmessages:OpenDataChannel"]
      Resource = "arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:session/$${aws:userid}-*"
    },
    {
      Sid       = "RegionalNodeDiscovery", Effect = "Allow", Action = "ssm:DescribeInstanceInformation", Resource = "*"
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
    }
  ] : statement if var.recovery_ssm != null]
  recovery_guard_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.recovery_session_statements, [
      {
        Sid       = "RegionalEC2Identity", Effect = "Allow", Action = "ec2:DescribeInstances", Resource = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid       = "VerifyAdministrativePolicyOwner", Effect = "Allow", Action = "s3:GetBucketPolicy", Resource = local.data_bucket_arn
        Condition = { Bool = { "aws:SecureTransport" = "true" }, StringEquals = { "s3:ResourceAccount" = var.aws_account_id } }
      },
      {
        Sid       = "DenyEveryOtherAPI", Effect = "Deny", Resource = "*"
        NotAction = concat(local.recovery_session_actions, ["ec2:DescribeInstances", "s3:GetBucketPolicy", "sts:GetCallerIdentity"])
      },
      { Sid = "DenyState", Effect = "Deny", Action = "s3:*", Resource = [local.state_bucket_arn, "${local.state_bucket_arn}/*"] }
    ])
  })
  recovery_agent_actions = ["ssm:UpdateInstanceInformation", "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"]
}

resource "aws_iam_policy" "deploy_guard_boundary" {
  count  = var.recovery_ssm == null ? 0 : 1
  name   = "${local.name}-deploy-guard-boundary"
  policy = local.recovery_guard_policy
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role" "deploy_guard" {
  count                = var.recovery_ssm == null ? 0 : 1
  name                 = "${local.name}-deploy-guard"
  permissions_boundary = aws_iam_policy.deploy_guard_boundary[0].arn
  max_session_duration = 7200
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc_provider_arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud"                 = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub"                 = local.github_subject
        "token.actions.githubusercontent.com:ref"                 = "refs/heads/${var.github_backup.branch}"
        "token.actions.githubusercontent.com:workflow"            = "Deploy platform (manual)"
        "token.actions.githubusercontent.com:repository_id"       = var.github_oidc.repository_id
        "token.actions.githubusercontent.com:repository_owner_id" = var.github_oidc.owner_id
      } }
    }]
  })
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role_policy" "deploy_guard" {
  count  = var.recovery_ssm == null ? 0 : 1
  name   = "scoped-recovery-interlock"
  role   = aws_iam_role.deploy_guard[0].id
  policy = local.recovery_guard_policy
}
resource "aws_iam_role_policy" "recovery_agent" {
  # Existing target only. The administrative plan must confirm the profile and
  # this role already exist; an installation without EC2 keeps this opt-in off.
  count = var.recovery_ssm == null ? 0 : 1
  name  = "minimal-ssm-recovery-channels"
  role  = "${local.name}-ec2"
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = local.recovery_agent_actions, Resource = "*"
    Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
  }] })
}
