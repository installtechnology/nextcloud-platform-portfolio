# Administrative destination only. Separate remote state required before any real plan/apply.
resource "terraform_data" "isolation" {
  input = var.data_bucket_name
  lifecycle {
    precondition {
      condition     = var.environment != var.source_environment && !contains([var.source_data_bucket_name, var.backups_bucket_name, var.state_bucket_name], var.data_bucket_name)
      error_message = "Destination must have independent environment and data bucket."
    }
    precondition {
      condition     = var.ec2_permissions_boundary_arn == "arn:aws:iam::${var.aws_account_id}:policy/${var.project_name}-${var.environment}-ec2-boundary"
      error_message = "Dedicated bootstrap-created destination boundary is mandatory."
    }
  }
}
resource "aws_s3_bucket" "data" {
  bucket        = var.data_bucket_name
  force_destroy = false
  lifecycle { prevent_destroy = true }
  depends_on = [terraform_data.isolation]
}
resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "data" {
  bucket = aws_s3_bucket.data.id
  rule { object_ownership = "BucketOwnerPreferred" }
}
resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "tls" {
  bucket = aws_s3_bucket.data.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*"
    Resource  = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role_policy" "ssm_agent" {
  name = "minimal-ssm-recovery-channels"
  role = aws_iam_role.host.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = ["ssm:UpdateInstanceInformation", "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"], Resource = "*"
    Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
  }] })
  depends_on = [aws_iam_role.host]
}
output "instance_id" { value = aws_instance.host.id }
output "public_ip" { value = aws_instance.host.public_ip }
output "destination_data_bucket" { value = aws_s3_bucket.data.id }
