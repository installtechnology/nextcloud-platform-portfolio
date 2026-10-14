output "data_bucket_name" { value = aws_s3_bucket.data.id }
output "data_bucket_arn" { value = aws_s3_bucket.data.arn }
output "backups_bucket_name" { value = aws_s3_bucket.backups.id }
output "backups_bucket_arn" { value = aws_s3_bucket.backups.arn }
output "bucket_region" { value = var.aws_region }
