# A boundary impede ampliar a role EC2 para state, backups ou administração IAM.
data "aws_iam_policy_document" "ec2_boundary" {
  dynamic "statement" {
    for_each = var.recovery_ssm == null ? [] : [1]
    content {
      sid       = "MinimumSSMAgentChannels"
      actions   = local.recovery_agent_actions
      resources = ["*"]
      condition {
        test     = "StringEquals"
        variable = "aws:RequestedRegion"
        values   = [var.aws_region]
      }
    }
  }
  statement {
    actions   = ["s3:GetBucketLocation", "s3:ListBucket"]
    resources = [local.data_bucket_arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = ["${local.data_bucket_arn}/*"]
  }
  statement {
    actions   = ["s3:PutObjectAcl"]
    resources = ["${local.data_bucket_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["private"]
    }
  }
}
resource "aws_iam_policy" "ec2_boundary" {
  name   = "${local.name}-ec2-boundary"
  policy = data.aws_iam_policy_document.ec2_boundary.json
  lifecycle { prevent_destroy = true }
}

locals {
  iam_host_arn           = "arn:${data.aws_partition.current.partition}:iam::${var.aws_account_id}:role/${local.name}-ec2"
  profile_arn            = "arn:${data.aws_partition.current.partition}:iam::${var.aws_account_id}:instance-profile/${local.name}-ec2"
  ec2_arn                = "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${var.aws_account_id}"
  operational_state_keys = [for root in ["persistent", "runtime"] : "${local.state_prefix}/${root}/terraform.tfstate"]
}

data "aws_iam_policy_document" "operational" {
  statement {
    sid       = "ListOperationalState"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket_arn]
    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values   = local.operational_state_keys
    }
  }
  statement {
    actions   = ["s3:GetBucketLocation"]
    resources = [local.state_bucket_arn]
  }
  statement {
    sid       = "OperationalStateOnly"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = [for key in local.operational_state_keys : "${local.state_bucket_arn}/${key}"]
  }
  statement {
    sid       = "OperationalLocksOnly"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [for key in local.operational_state_keys : "${local.state_bucket_arn}/${key}.tflock"]
  }
  statement {
    sid = "ManageTwoApplicationBucketsConfiguration"
    actions = [
      "s3:CreateBucket", "s3:ListBucket", "s3:GetBucketLocation",
      "s3:GetBucketAcl", "s3:GetBucketPolicy",
      "s3:GetBucketPublicAccessBlock", "s3:PutBucketPublicAccessBlock",
      "s3:GetBucketOwnershipControls", "s3:PutBucketOwnershipControls",
      "s3:GetEncryptionConfiguration", "s3:PutEncryptionConfiguration",
      "s3:GetBucketVersioning", "s3:PutBucketVersioning",
      "s3:GetBucketTagging", "s3:PutBucketTagging",
      "s3:GetLifecycleConfiguration", "s3:GetBucketLogging", "s3:GetBucketWebsite",
      "s3:GetReplicationConfiguration", "s3:GetBucketObjectLockConfiguration",
      "s3:GetAccelerateConfiguration", "s3:GetBucketRequestPayment", "s3:GetBucketCORS"
    ]
    resources = [local.data_bucket_arn, local.backups_bucket_arn]
  }
  statement {
    sid       = "ManageBackupBucketPolicyOnly"
    actions   = ["s3:PutBucketPolicy"]
    resources = [local.backups_bucket_arn]
  }
  statement {
    sid       = "DenyDirectDataPolicyMutation"
    effect    = "Deny"
    actions   = ["s3:PutBucketPolicy", "s3:DeleteBucketPolicy"]
    resources = [local.data_bucket_arn]
  }
  statement {
    sid = "RegionalReadOnlyEC2"
    actions = [
      "ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ec2:DescribeInstanceAttribute",
      "ec2:DescribeInstanceCreditSpecifications", "ec2:DescribeInstanceTypes", "ec2:DescribeVolumes", "ec2:DescribeVolumesModifications", "ec2:DescribeImages",
      "ec2:DescribeIamInstanceProfileAssociations",
      "ec2:DescribeVpcs", "ec2:DescribeVpcAttribute", "ec2:DescribeSubnets",
      "ec2:DescribeAvailabilityZones", "ec2:DescribeInternetGateways", "ec2:DescribeRouteTables",
      "ec2:DescribeSecurityGroups", "ec2:DescribeSecurityGroupRules", "ec2:DescribeKeyPairs",
      "ec2:DescribeNetworkInterfaces", "ec2:DescribeTags"
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }
  statement {
    sid     = "CreateTaggedRuntime"
    actions = ["ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateInternetGateway", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup", "ec2:ImportKeyPair"]
    # RequestTag vale para os recursos novos. A VPC dependente é autorizada abaixo com ResourceTag.
    resources = ["${local.ec2_arn}:vpc/*", "${local.ec2_arn}:subnet/*", "${local.ec2_arn}:internet-gateway/*", "${local.ec2_arn}:route-table/*", "${local.ec2_arn}:security-group/*", "${local.ec2_arn}:key-pair/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [var.project_name]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Environment"
      values   = [var.environment]
    }
  }
  statement {
    sid       = "CreateResourcesInTaggedVpc"
    actions   = ["ec2:CreateSubnet", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup"]
    resources = ["${local.ec2_arn}:vpc/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.project_name]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Environment"
      values   = [var.environment]
    }
  }
  statement {
    # TagSpecifications exige autorização da regra nova, além do SG existente em ManageTaggedRuntime.
    sid       = "CreateTaggedSecurityGroupRules"
    actions   = ["ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress"]
    resources = ["${local.ec2_arn}:security-group-rule/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [var.project_name]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Environment"
      values   = [var.environment]
    }
  }
  statement {
    sid       = "RunTaggedInstanceAndVolume"
    actions   = ["ec2:RunInstances"]
    resources = ["${local.ec2_arn}:instance/*", "${local.ec2_arn}:volume/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [var.project_name]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Environment"
      values   = [var.environment]
    }
  }
  statement {
    sid       = "RunInstanceDependencies"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}::image/*", "${local.ec2_arn}:network-interface/*", "${local.ec2_arn}:subnet/*", "${local.ec2_arn}:security-group/*", "${local.ec2_arn}:key-pair/*"]
  }
  statement {
    sid       = "TagOnCreation"
    actions   = ["ec2:CreateTags"]
    resources = ["${local.ec2_arn}:*/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["CreateVpc", "CreateSubnet", "CreateInternetGateway", "CreateRouteTable", "CreateSecurityGroup", "ImportKeyPair", "RunInstances", "AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress"]
    }
  }
  statement {
    sid = "ManageTaggedRuntime"
    actions = [
      "ec2:ModifyVpcAttribute", "ec2:DeleteVpc", "ec2:ModifySubnetAttribute", "ec2:DeleteSubnet",
      "ec2:AttachInternetGateway", "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway",
      "ec2:CreateRoute", "ec2:ReplaceRoute", "ec2:DeleteRoute", "ec2:AssociateRouteTable",
      "ec2:DisassociateRouteTable", "ec2:ReplaceRouteTableAssociation", "ec2:DeleteRouteTable",
      "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress",
      "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress", "ec2:ModifySecurityGroupRules", "ec2:DeleteSecurityGroup",
      "ec2:DeleteKeyPair", "ec2:ModifyInstanceAttribute", "ec2:ModifyInstanceCreditSpecification",
      "ec2:ModifyInstanceMetadataOptions", "ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances",
      "ec2:ModifyVolume", "ec2:CreateTags", "ec2:DeleteTags", "ec2:GetConsoleOutput",
      "ec2:AssociateIamInstanceProfile", "ec2:DisassociateIamInstanceProfile", "ec2:ReplaceIamInstanceProfileAssociation"
    ]
    resources = ["${local.ec2_arn}:vpc/*", "${local.ec2_arn}:subnet/*", "${local.ec2_arn}:internet-gateway/*", "${local.ec2_arn}:route-table/*", "${local.ec2_arn}:security-group/*", "${local.ec2_arn}:security-group-rule/*", "${local.ec2_arn}:key-pair/*", "${local.ec2_arn}:instance/*", "${local.ec2_arn}:volume/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.project_name]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Environment"
      values   = [var.environment]
    }
  }
  statement {
    sid       = "CanonicalAMIOnly"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::parameter/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"]
  }
  statement {
    sid       = "CreateHostRoleWithBoundary"
    actions   = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]
    resources = [local.iam_host_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.ec2_boundary.arn]
    }
  }
  statement {
    sid       = "ManageExactHostRole"
    actions   = ["iam:GetRole", "iam:DeleteRole", "iam:UpdateAssumeRolePolicy", "iam:UpdateRole", "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy"]
    resources = [local.iam_host_arn]
  }
  statement {
    sid       = "ManageExactHostProfile"
    actions   = ["iam:CreateInstanceProfile", "iam:GetInstanceProfile", "iam:DeleteInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:TagInstanceProfile", "iam:UntagInstanceProfile", "iam:ListInstanceProfileTags"]
    resources = [local.profile_arn]
  }
  statement {
    actions   = ["iam:ListInstanceProfilesForRole"]
    resources = [local.iam_host_arn]
  }
  statement {
    sid       = "PassExactHostRoleToEC2Only"
    actions   = ["iam:PassRole"]
    resources = [local.iam_host_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }
}
resource "aws_iam_role_policy" "operational" {
  count  = var.github_oidc == null ? 0 : 1
  name   = "persistent-runtime-deploy"
  role   = aws_iam_role.github[0].id
  policy = data.aws_iam_policy_document.operational.json
}
