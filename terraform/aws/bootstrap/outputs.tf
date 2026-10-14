output "github_role_arn" {
  description = "Role operacional persistent/runtime; sem acesso ao state bootstrap ou backup/restore."
  value       = try(aws_iam_role.github[0].arn, null)
}

output "github_oidc_provider_arn" {
  description = "Provider criado ou pertencente ao cliente, quando OIDC habilitado."
  value       = local.oidc_provider_arn
}

output "backup_restore_policy_arn" { value = aws_iam_policy.backup_restore.arn }

output "state_bucket_name" { value = aws_s3_bucket.state.id }
output "state_prefix" { value = local.state_prefix }
output "github_subject" { value = local.github_subject }
output "ec2_permissions_boundary_arn" { value = aws_iam_policy.ec2_boundary.arn }

output "backup_role_arn" {
  description = "Role exclusiva backup; null enquanto opt-in não configurado/aplicado."
  value       = try(aws_iam_role.backup[0].arn, null)
}

output "backup_permissions_boundary_arn" {
  value = try(aws_iam_policy.backup_boundary[0].arn, null)
}

output "backup_oidc_requirements" {
  description = "Pré-requisitos do futuro workflow/Environment; não configura GitHub nem autoriza operação."
  value = local.backup_enabled ? {
    subject        = local.github_subject
    branch         = var.github_backup.branch
    workflow_name  = var.github_backup.workflow_name
    environment    = var.github_oidc.environment
    backups_prefix = "backups/"
  } : null
}
output "data_policy_admin_role_arn" {
  description = "Administrative policy-only role; null means not provisioned."
  value       = one(aws_iam_role.data_policy_admin[*].arn)
}
output "deploy_guard_role_arn" {
  value       = var.recovery_ssm == null ? null : aws_iam_role.deploy_guard[0].arn
  description = "Separate OIDC guard role; does not provision, write S3 or administer IAM."
}
