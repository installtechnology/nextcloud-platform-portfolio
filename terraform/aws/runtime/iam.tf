data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.${data.aws_partition.current.dns_suffix}"]
    }
  }
}

resource "aws_iam_role" "host" {
  name                 = "${local.name}-ec2"
  permissions_boundary = var.ec2_permissions_boundary_arn
  assume_role_policy   = data.aws_iam_policy_document.ec2_trust.json
}

data "aws_iam_policy_document" "storage" {
  statement {
    sid       = "BucketLocation"
    actions   = ["s3:GetBucketLocation"]
    resources = [local.data_bucket_arn]
  }
  statement {
    sid       = "ListDataBucket"
    actions   = ["s3:ListBucket"]
    resources = [local.data_bucket_arn]
  }
  statement {
    sid       = "PrimaryObjectStorage"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = ["${local.data_bucket_arn}/*"]
  }
  statement {
    sid       = "NextcloudPrivateAclOnly"
    actions   = ["s3:PutObjectAcl"]
    resources = ["${local.data_bucket_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["private"]
    }
  }
}

resource "aws_iam_role_policy" "storage" {
  name   = "scoped-storage"
  role   = aws_iam_role.host.id
  policy = data.aws_iam_policy_document.storage.json
}

resource "aws_iam_instance_profile" "host" {
  name = "${local.name}-ec2"
  role = aws_iam_role.host.name
}
