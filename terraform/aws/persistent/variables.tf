variable "aws_region" {
  description = "Região AWS escolhida pelo operador, sem default."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "Informe uma região AWS válida."
  }
}

variable "project_name" {
  description = "Identificador do projeto para tags."
  type        = string
}

variable "environment" {
  description = "Identificador do ambiente."
  type        = string
}

variable "data_bucket_name" {
  description = "Nome globalmente único do bucket exclusivo de dados Nextcloud; informado pelo cliente."
  type        = string
  validation {
    condition     = length(var.data_bucket_name) >= 3 && length(var.data_bucket_name) <= 63 && can(regex("^[a-z0-9][a-z0-9-]*[a-z0-9]$", var.data_bucket_name))
    error_message = "Use 3 a 63 caracteres minúsculos, números e hífens, iniciando/terminando com letra ou número."
  }
}

variable "backups_bucket_name" {
  description = "Nome globalmente único do bucket exclusivo de backups manuais; informado pelo cliente."
  type        = string
  validation {
    condition     = var.data_bucket_name != var.backups_bucket_name
    error_message = "Dados e backups exigem buckets distintos."
  }
  validation {
    condition     = length(var.backups_bucket_name) >= 3 && length(var.backups_bucket_name) <= 63 && can(regex("^[a-z0-9][a-z0-9-]*[a-z0-9]$", var.backups_bucket_name))
    error_message = "Use 3 a 63 caracteres minúsculos, números e hífens, iniciando/terminando com letra ou número."
  }
}

variable "tags" {
  description = "Tags adicionais sem informações sensíveis."
  type        = map(string)
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
