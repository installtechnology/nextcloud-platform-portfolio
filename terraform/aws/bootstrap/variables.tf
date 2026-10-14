variable "aws_region" {
  description = "Região AWS definida pelo operador."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "Informe uma região AWS válida."
  }
}

variable "project_name" {
  description = "Prefixo dos recursos administrativos de automação."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,24}$", var.project_name))
    error_message = "Use 2 a 25 caracteres minúsculos/números/hífens, iniciando com letra."
  }
}

variable "environment" {
  description = "Ambiente utilizado no nome dos recursos."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,14}$", var.environment))
    error_message = "Use 2 a 15 caracteres minúsculos/números/hífens, iniciando com letra."
  }
}

variable "backups_bucket_name" {
  description = "Nome do bucket exclusivo de backups; compõe ARNs sem depender do runtime ou consultar S3."
  type        = string
}

variable "tags" {
  type        = map(string)
  description = "Tags adicionais."
  default     = {}
}


variable "aws_account_id" {
  description = "Conta esperada, verificada pelo provider; sem default."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Informe o ID AWS esperado com 12 dígitos."
  }
}

variable "data_bucket_name" {
  type = string
  validation {
    condition     = var.data_bucket_name != var.backups_bucket_name
    error_message = "Dados e backups devem ter nomes distintos também no bootstrap."
  }
}
variable "state_bucket_name" {
  type = string
  validation {
    condition     = var.state_bucket_name != var.data_bucket_name && var.state_bucket_name != var.backups_bucket_name && can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "State exige um terceiro bucket válido, distinto de dados e backups."
  }
}
variable "github_oidc" {
  description = "Configuração exata do repositório e environment; null não cria identidade GitHub. IDs opcionais selecionam o formato imutável do sub."
  type = object({
    owner                 = string
    repository            = string
    environment           = string
    owner_id              = optional(string)
    repository_id         = optional(string)
    existing_provider_arn = optional(string)
  })
  default = null
  validation {
    condition = var.github_oidc == null ? true : alltrue([
      can(regex("^[A-Za-z0-9-]+$", var.github_oidc.owner)),
      can(regex("^[A-Za-z0-9_.-]+$", var.github_oidc.repository)),
      var.github_oidc.environment == var.environment,
      (var.github_oidc.owner_id == null && var.github_oidc.repository_id == null) || (can(regex("^[0-9]+$", var.github_oidc.owner_id)) && can(regex("^[0-9]+$", var.github_oidc.repository_id)))
    ])
    error_message = "Owner/repository exatos, environment igual ao root e IDs numéricos fornecidos juntos; sem curingas."
  }
  validation {
    condition     = var.github_oidc == null ? true : (var.github_oidc.existing_provider_arn == null ? true : var.github_oidc.existing_provider_arn == "arn:aws:iam::${var.aws_account_id}:oidc-provider/token.actions.githubusercontent.com")
    error_message = "Provider GitHub existente deve pertencer à conta esperada (partição comercial AWS)."
  }
}

variable "github_backup" {
  description = "Opt-in PR4: branch e nome exatos do futuro workflow manual; null mantém bootstrap sem role de backup. Requer OIDC com IDs imutáveis e Environment protegido."
  type = object({
    branch        = string
    workflow_name = string
  })
  default = null
  validation {
    condition = var.github_backup == null ? true : (
      var.github_oidc == null ? false : var.github_oidc.owner_id != null && var.github_oidc.repository_id != null
    )
    error_message = "Backup exige github_oidc explícito com owner_id e repository_id imutáveis."
  }
  validation {
    condition = var.github_backup == null ? true : (
      can(regex("^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$", var.github_backup.branch)) &&
      !can(regex("(\\.\\.|//|(^|/)\\.|\\.lock($|/)|[./]$)", var.github_backup.branch)) &&
      can(regex("^[A-Za-z0-9][A-Za-z0-9 ()_-]{1,79}$", var.github_backup.workflow_name))
    )
    error_message = "Informe branch literal válida e nome exato do workflow (2 a 80 caracteres), sem curingas/controles."
  }
  validation {
    condition = var.github_backup == null ? true : (
      can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.data_bucket_name)) &&
      can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.backups_bucket_name))
    )
    error_message = "Backup exige nomes literais de buckets (3 a 63 caracteres), sem curingas em ARNs."
  }
}

variable "data_policy_authority" {
  description = "Opt-in: trusted administrative broker role, distinct from deploy/backup/EC2. No OIDC workflow or activation is created."
  type        = object({ issuer_role_arn = string })
  default     = null
  validation {
    condition = var.data_policy_authority == null ? true : (
      can(regex("^arn:aws:iam::${var.aws_account_id}:role/[A-Za-z0-9_+=,.@/-]+$", var.data_policy_authority.issuer_role_arn)) &&
      !contains([for suffix in ["github", "backup", "ec2", "data-policy-admin", "deploy-guard"] : "arn:aws:iam::${var.aws_account_id}:role/${var.project_name}-${var.environment}-${suffix}"], var.data_policy_authority.issuer_role_arn)
    )
    error_message = "Use a distinct, same-account administrative broker role; never deploy, backup, EC2 or self trust."
  }
}

variable "recovery_ssm" {
  description = "Reviewed opt-in for an existing recovery target. Creates separate guard identity and minimum agent channel policy; no activation/install."
  type        = object({ instance_id = string })
  default     = null
  validation {
    condition = var.recovery_ssm == null ? true : (
      can(regex("^i-[a-f0-9]{17}$", var.recovery_ssm.instance_id)) && var.github_backup != null &&
      (var.github_oidc == null ? false : var.github_oidc.repository_id != null && var.github_oidc.owner_id != null)
    )
    error_message = "Recovery SSM requires a literal existing target, reviewed backup OIDC and immutable repository IDs."
  }
}

variable "github_restore" {
  description = "Opt-in PR8. Destination boundary first; exact target ID enables dedicated restore OIDC role later."
  type = object({
    destination_environment = string
    destination_bucket_name = string
    target_instance_id      = optional(string)
  })
  default = null
  validation {
    condition = var.github_restore == null ? true : (
      can(regex("^[a-z][a-z0-9-]{1,14}$", var.github_restore.destination_environment)) &&
      var.github_restore.destination_environment != var.environment &&
      can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.github_restore.destination_bucket_name)) &&
      !contains([var.data_bucket_name, var.backups_bucket_name, var.state_bucket_name], var.github_restore.destination_bucket_name) &&
      (var.github_restore.target_instance_id == null ? true : can(regex("^i-[a-f0-9]{17}$", var.github_restore.target_instance_id)))
    )
    error_message = "Restore requires a separate environment/data bucket and optional exact destination instance ID."
  }
  validation {
    condition     = var.github_restore == null ? true : (var.github_oidc == null ? false : var.github_oidc.repository_id != null && var.github_oidc.owner_id != null)
    error_message = "Restore requires the existing OIDC configuration with immutable repository identifiers."
  }
}
variable "github_reprovision" {
  description = "Opt-in exclusivo para criação protegida de destino independente; null preserva o deploy existente. Emissora administrativa existente e boundary do destino exigem revisão separada."
  type = object({
    destination_environment = string
    destination_data_bucket = string
    grant_issuer_arn        = string
  })
  default = null
  validation {
    condition = var.github_reprovision == null ? true : (
      var.github_oidc != null && try(var.github_oidc.repository_id, null) != null && try(var.github_oidc.owner_id, null) != null &&
      var.github_restore != null && try(var.github_reprovision.destination_environment == var.github_restore.destination_environment, false) &&
      try(var.github_reprovision.destination_data_bucket == var.github_restore.destination_bucket_name, false) &&
      var.github_reprovision.destination_environment != var.environment &&
      can(regex("^[a-z][a-z0-9-]{1,14}$", var.github_reprovision.destination_environment)) &&
      !contains([var.data_bucket_name, var.backups_bucket_name, var.state_bucket_name], var.github_reprovision.destination_data_bucket) &&
      !contains([for name in ["github", "reprovision", "backup", "restore", "deploy-guard", "ec2"] : "arn:aws:iam::${var.aws_account_id}:role/${var.project_name}-${var.environment}-${name}"], var.github_reprovision.grant_issuer_arn) &&
      can(regex("^arn:aws:iam::${var.aws_account_id}:role/[A-Za-z0-9+=,.@_/-]+$", var.github_reprovision.grant_issuer_arn))
    )
    error_message = "Reprovision requires immutable OIDC, existing dedicated restore boundary, independent environment/bucket and an exact same-account administrative issuer."
  }
}
