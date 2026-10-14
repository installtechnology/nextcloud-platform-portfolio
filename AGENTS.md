# Regras do projeto

- Implementar somente a fase autorizada e parar ao final dela.
- Nunca executar `terraform apply` ou `terraform destroy` sem autorização explícita.
- Executar `terraform fmt -check -recursive` e `terraform validate` em cada root alterado.
- Não commitar state, planos salvos, credenciais, chaves privadas, `.env` real ou tfvars reais.
- Preferir OIDC, sessões federadas/SSO e instance profiles; nunca credenciais AWS permanentes.
- Manter AWS region, nomes, conta e parâmetros de cliente configuráveis.
- Priorizar custo baixo; justificar qualquer aumento de instância ou serviço pago.
- Os roots `terraform/aws/bootstrap` e `terraform/aws/persistent` não fazem parte do destroy operacional; somente `runtime` pode ser alvo.
- OIDC e role do GitHub pertencem exclusivamente a bootstrap, sem dependência do runtime.
- Backend: S3 persistente dedicado, state por conta/ambiente/root e locking nativo `use_lockfile = true`; não adicionar DynamoDB.
- Não habilitar/migrar backend ou provisionar seu bucket sem autorização. Runners nunca podem usar state local para operação real.
- Nunca habilitar `force_destroy`, expiração ou exclusão automática de backups no S3.
- Dados Nextcloud, backups e state exigem buckets distintos. Role EC2 da aplicação não acessa backups/state. Primary Object Storage usa IAM temporário, sem keys e sem migração implícita de instalação local.
- Não adicionar lifecycle/expiração automática em dados ou backups. Nextcloud 34 requer ACL private: dados usam BucketOwnerPreferred, IAM PutObjectAcl condicionado a private e bloqueios públicos integrais; backups mantêm BucketOwnerEnforced.
- Backup é exclusivamente manual; deploy, destroy e DR não geram backup.
- Destroy futuro deve abortar sem pelo menos um conjunto de backup completo e válido.
- DR específico nunca pode cair silenciosamente em `latest` nem combinar backups.
- Terraform configura infraestrutura; Ansible configura o sistema; Compose executa serviços.
- Não implementar Azure até autorização; manter apenas placeholder.
- Observabilidade é local e descartável, sem S3 ou inclusão em backup/DR. Grafana publica TCP 3000 somente em IPv4, com autenticação obrigatória e SG restrito aos HTTP_ALLOWED_CIDRS administrativos (nunca /0). Mantém observability internal e bridge grafana_access dedicada. Coletores sem portas públicas, Docker socket ou containers privileged. Não ampliar IAM da EC2 para observabilidade. Descobrir IP público por outputs runtime/API; nunca fixar destino no código.
- Documentar comandos, resultados, riscos, decisões e pendências ao final de cada fase.

- Bucket de state pertence ao bootstrap; backend.tf é gerado a partir de template somente após bootstrap/migração autorizados. CI nunca opera bootstrap.
- deploy.yml é manual, default plan; apply exige confirmation=APPLY, identidade/região verificadas, sizing e buckets visíveis. Nunca upload de state/planos/chaves.
- IAM GitHub não pode alterar sua própria role/OIDC/boundary/bucket de state; PassRole exato para EC2 e boundary EC2 restrita a dados. Backup/restore continua sem attachment.
- SSH CI é temporário TCP/22 /32, com ownership/timestamp do workflow, cleanup always e coleta conservadora de regras antigas. Nunca revogar CIDRs administrativos ou desabilitar host key checking.
- Default root gp3 criptografado segue variável Terraform; não repetir default de sizing em tfvars/workflows. Alterações futuras exigem medição/justificativa.
