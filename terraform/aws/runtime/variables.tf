variable "aws_region" {
  description = "Região AWS definida pelo operador."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "Informe uma região AWS válida."
  }
}

variable "project_name" {
  description = "Prefixo dos nomes de recursos temporários."
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

variable "data_bucket_name" {
  description = "Bucket criado no root persistent, na mesma região/conta."
  type        = string
}

variable "vpc_cidr" {
  type        = string
  description = "Rede IPv4 privada do laboratório."
  default     = "10.42.0.0/16"
  validation {
    condition     = can(cidrnetmask(var.vpc_cidr))
    error_message = "Informe um CIDR IPv4 válido."
  }
}

variable "subnet_cidr" {
  type        = string
  description = "Subnet pública dentro da VPC."
  default     = "10.42.1.0/24"
  validation {
    condition     = can(cidrnetmask(var.subnet_cidr))
    error_message = "Informe um CIDR IPv4 válido."
  }
}

variable "availability_zone" {
  type        = string
  description = "AZ opcional; null seleciona a primeira AZ disponível na região."
  default     = null
}

variable "ssh_allowed_cidrs" {
  type        = set(string)
  description = "CIDRs IPv4 administrativos; nunca liberar SSH para toda a Internet."
  validation {
    condition     = length(var.ssh_allowed_cidrs) > 0 && alltrue([for c in var.ssh_allowed_cidrs : can(cidrnetmask(c)) && try(tonumber(split("/", c)[1]) >= 24, false)])
    error_message = "Informe CIDRs IPv4 /24 ou mais restritos para SSH."
  }
}

variable "http_allowed_cidrs" {
  type        = set(string)
  description = "CIDRs administrativos IPv4 para HTTP do Nextcloud e Grafana; /0 proibido."
  validation {
    condition     = length(var.http_allowed_cidrs) > 0 && alltrue([for c in var.http_allowed_cidrs : can(cidrnetmask(c)) && try(tonumber(split("/", c)[1]) > 0, false)])
    error_message = "Informe CIDRs administrativos IPv4 para HTTP/Grafana, sem acesso mundial /0."
  }
}

variable "ssh_public_key" {
  type        = string
  description = "Somente chave pública OpenSSH. A chave privada fica com o operador."
  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+) [A-Za-z0-9+/=]+", trimspace(var.ssh_public_key)))
    error_message = "Informe uma chave pública OpenSSH válida; nunca uma chave privada."
  }
}

variable "instance_type" {
  type        = string
  description = "T3 x86_64 compatível com AMI/credit_specification deste root; aumentar somente com justificativa técnica."
  default     = "t3.medium"
  validation {
    condition     = can(regex("^t3\\.(nano|micro|small|medium|large|xlarge|2xlarge)$", var.instance_type))
    error_message = "Este template aceita T3 x86_64. Outra família exige revisar arquitetura da AMI e política de créditos."
  }
}

variable "ubuntu_ami_ssm_parameter" {
  type        = string
  description = "AMI Ubuntu LTS via catálogo público Canonical; arquitetura deve corresponder à instância."
  default     = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

variable "root_volume_size_gib" {
  type        = number
  description = "Disco gp3 para SO e futura stack; dados de usuário ficam no S3."
  default     = 30
  validation {
    condition     = var.root_volume_size_gib >= 8 && var.root_volume_size_gib <= 30 && floor(var.root_volume_size_gib) == var.root_volume_size_gib
    error_message = "O template de baixo custo aceita 8 a 30 GiB inteiros."
  }
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

variable "ec2_permissions_boundary_arn" {
  description = "Boundary criada pelo bootstrap; null permitido somente em validação/local administrada. CI exige o ARN exato."
  type        = string
  default     = null
}
