# Bucket administrativo, criado inicialmente com state local.
resource "aws_s3_bucket" "state" {
  bucket        = var.state_bucket_name
  force_destroy = false
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

data "aws_iam_policy_document" "state_tls" {
  dynamic "statement" {
    for_each = var.github_reprovision == null ? [] : [var.github_reprovision]
    content {
      sid       = "ExclusiveRecoveryCreationJournalWriter"
      effect    = "Deny"
      actions   = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
      resources = ["${aws_s3_bucket.state.arn}/${local.creation_prefix}/lease.json"]
      principals {
        type        = "*"
        identifiers = ["*"]
      }
      condition {
        test     = "ArnNotEquals"
        variable = "aws:PrincipalArn"
        values   = [statement.value.grant_issuer_arn, local.creation_role_arn]
      }
    }
  }
  dynamic "statement" {
    for_each = var.github_reprovision == null ? [] : [var.github_reprovision]
    content {
      sid       = "ExclusiveRecoveryGrantIssuer"
      effect    = "Deny"
      actions   = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
      resources = ["${aws_s3_bucket.state.arn}/${local.creation_prefix}/grants/*"]
      principals {
        type        = "*"
        identifiers = ["*"]
      }
      condition {
        test     = "ArnNotEquals"
        variable = "aws:PrincipalArn"
        values   = [statement.value.grant_issuer_arn]
      }
    }
  }
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state_tls" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_tls.json
}
