# GitHub Actions

## Estado final do laboratório

Deploy, backup operacional e restaurações foram executados nas Fases 6 e 7.
As VMs foram removidas posteriormente por planos administrativos revisados.
Os parágrafos de PRs abaixo preservam o histórico de suas entregas, não o
estado final. Não existe workflow Destroy concluído. Consulte o
[resultado do portfólio](../../docs/portfolio-evidence.md) e a
[revisão de publicação](../../docs/publication-review.md) para o estado vigente.

Deploy real em deploy.yml, OIDC, SSH, Terraform remoto e Ansible foram
validados com sucesso no laboratório. Resultado sanitizado na [Fase 6](../../docs/phase-6.md).
PR6 prepara [backup.yml](backup.yml), que sempre recusa operação sem OIDC/Secrets,
e [backup-posix-validation.yml](backup-posix-validation.yml), manual com fixtures locais,
sem execução autorizada nesta entrega. Nenhum backup operacional, destroy/DR/Azure.
Decisões de conectividade, blockers e aceite: [PR6](../../docs/phase-7-pr6.md).

## Configuração externa

Repositório público ou privado, owner/repo configuráveis. Bootstrap SSO/federado
e migração autorizada precedem CI: [roteiro](../../docs/terraform-state.md).
Criar GitHub Environment com nome igual ao environment Terraform/OIDC; restringir
deployment branches à DEPLOY_BRANCH e proteger workflow/código nessa branch.
Usar revisão/aprovação quando disponível no plano GitHub. Proteções de environments
privados dependem do plano: verificar antes de habilitar apply. Input APPLY não
substitui controle de quem modifica workflows ou acessa secrets.

AWS trust: StringEquals aud=sts.amazonaws.com e sub exato repo/environment.
owner_id/repository_id juntos selecionam formato imutável; omitir ambos somente
se repo usa formato clássico. Confirmar formato, especialmente em repos novos
em 2026. Environment sub não contém branch; proteção GitHub e guard DEPLOY_BRANCH
restringem branch. Não usar wildcard para contornar falha OIDC.

## Modos e guards

workflow_dispatch apenas; environment default lab, mode default plan. Apply
exige confirmation exatamente APPLY, guard antes de OIDC e novamente antes de
cada apply. Não há trigger push/PR ou provisão silenciosa.

Plan: checkout/inputs, setup Terraform/OIDC, instalação controller e syntax-check/
lint, STS/região do bucket state, manifesto conta/região/environment/sizing/buckets,
init remoto/fmt/validate/plan persistent e runtime. Sem VM/Ansible real/criação
de recursos. Backend grava/remove locks; requests S3/minutos GitHub podem custar.

Apply: mesmos guards; plan+apply persistent, depois plan+apply runtime. Plano
privado aplicado na mesma execução, nunca artifact. Outputs → EC2/SSH → playbook
Ansible existente → healthchecks das oito imagens → URLs atuais Nextcloud/Grafana. Sem
rollback/destroy automático: falha Ansible deixa infraestrutura criada e custos.

## Variables (não secretas)

| Nome | Contrato/origem |
| --- | --- |
| AWS_ACCOUNT_ID | 12 dígitos, conta esperada |
| AWS_REGION | Região única recursos/backend |
| AWS_ROLE_ARN | github_role_arn bootstrap; formato conta/projeto/environment conferido |
| PROJECT_NAME | Projeto idêntico ao bootstrap |
| STATE_BUCKET_NAME | Output state_bucket_name bootstrap |
| DATA_BUCKET_NAME, BACKUPS_BUCKET_NAME | Nomes do bootstrap; três buckets distintos |
| DEPLOY_BRANCH | Branch exata sem refs/heads/, protegida no Environment |
| SSH_PUBLIC_KEY | Ed25519 correspondente ao Secret privado |
| SSH_ALLOWED_CIDRS | Array JSON administrativo, /24 ou mais restrito |
| HTTP_ALLOWED_CIDRS | Array JSON dos CIDRs administrativos IPv4 para Nextcloud TCP/80 e Grafana TCP/3000, sem /0 |
| NEXTCLOUD_ADMIN_USER, GRAFANA_ADMIN_USER | Usuários conforme contrato Ansible |
| INSTANCE_TYPE | Opcional; vazio herda default Terraform t3.medium |
| ROOT_VOLUME_SIZE_GIB | Opcional; vazio herda default Terraform; até o limite autorizado do root |

Não repetir defaults de sizing em Variables/tfvars para simular herança. Root gp3
criptografado default agora 30 GiB, sem volume adicional. Tfvars JSON CI são gerados
das Variables, sem passwords. bootstrap.tfvars é administrativo fora do workflow;
exemplos dos dois ambientes/três roots incluem aws_account_id.

## Secrets

SSH_PRIVATE_KEY, NEXTCLOUD_ADMIN_PASSWORD, POSTGRES_ADMIN_PASSWORD, POSTGRES_PASSWORD,
REDIS_PASSWORD, GRAFANA_ADMIN_PASSWORD. Nenhum valor real no Git. Senhas 32–256
caracteres A–Z/a–z/0–9/_/-, não iniciar REPLACE; PostgreSQL admin e aplicação
distintos. Preflight valida antes de provisionar. Senhas só no ambiente do passo
Ansible/no_log, não via CLI extra-vars. Rotação acidental continua bloqueada.

Chave privada dedicada ao laboratório, sem passphrase para uso não interativo,
armazenada no Environment protegido. Rotação/desativação exige procedimento próprio.
Sem Access Key/Secret Key AWS permanente; OIDC sessão de 2 horas. Não publicar .env/plan/state/
chaves como artifact. Sessão AWS temporária contém credentials no ambiente do runner.

## SSH temporário e verificação

IPv4 global via HTTPS checkip.amazonaws.com → TCP/22 /32 no SG da instância esperada.
Antes da abertura, EC2 API confere OwnerId/tags/IP/SG. Regra fora do Terraform,
descrição nextcloud-deploy:<run>:<attempt> e tags Workflow/Repository/Project/
Environment/Run/CreatedAt. SG usa regras standalone, preservando ingress CI.
Regra idêntica existente é reutilizada sem modificar ownership ou revogar.

Chave privada 0600 em RUNNER_TEMP/platform-deploy (diretório pai 0700); ssh-keygen compara
pública correspondente sem imprimir. ssh-keyscan não é confiança: aceita Ed25519
apenas se SHA256 corresponde a SSH HOST KEY FINGERPRINTS obtido por GetConsoleOutput
autenticado do instance_id esperado. Console não é impresso. Aguarda até 10 minutos;
ausência/mismatch aborta. StrictHostKeyChecking yes, BatchMode e IdentitiesOnly.
Sem TOFU/fallback. Ubuntu/cloud-init deve publicar fingerprints; console pode não
reter fingerprint após reboot. Intervenção administrativa se faltar. Console não
atesta que a VM não foi comprometida; AMI dinâmica também requer revisão.

Cleanup final if:always revoga só regras deste run+attempt identificadas e remove
chave/known_hosts/inventário/tfvars/backend/planos mesmo se AWS cleanup falhar.
Erro de revogação deixa job em erro. Próximo apply serializado remove regras do
mesmo workflow/repo/projeto/environment com idade>3h e identidade /32/tcp22 exata.
Não revoga regras administrativas/outros projetos/regras sem tags/leases recentes.
Timeout de 90 minutos, sessão de 2 horas, janela de 3 horas conservadora: revisar juntos antes de alterar.
IAM permite gerenciar ingress/egress no SG do projeto para Terraform; /32/tcp22/
ownership da regra temporária são restrições do código CI, não toda a policy IAM.

Always não garante cleanup em perda do runner, término forçado ou credentials
revogadas. Operador deve inspecionar tags Workflow/Repository/Run e revogar o
SecurityGroupRuleId confirmado se não houver run ativo. Nunca revogar CIDRs
administrativos. Grafana usa TCP/3000 restrito a HTTP_ALLOWED_CIDRS. A regra
temporária do runner continua somente TCP/22; não autoriza 3000. Probes Grafana
rodam dentro da EC2, sem comprovar acesso externo. Destino/URLs vêm dos outputs
runtime, sem Variable com IP fixo. Ver [revisão Grafana](../../docs/grafana-browser-access.md).

## Ferramentas e validação

Ubuntu 24.04; Terraform 1.14.2; Actions checkout7.0.1/setup-terraform4.0.1/
configure-aws-credentials6.3.0 fixadas por SHA. Venv requirements-control.txt
(ansible-core2.19.x/ansible-lint25.x) e community.docker5.3.0. No diretório ansible:

```bash
ansible-playbook -i inventory/hosts.yml.example playbooks/deploy.yml --syntax-check
ansible-lint -c ../.ansible-lint playbooks/deploy.yml
```

Esses checks precedem apply; playbook real apenas mode=apply. Probes existentes
validam Nextcloud/SQL/Redis/S3, readiness Prometheus/Loki/Alloy/Node Exporter e
Grafana health/datasources/dashboard. Instalação não é duplicada em shell GitHub.
Local Windows: actionlint, YAML/HCL, guards/host key/cleanup offline, Terraform
mock e testes anteriores; Linux lint/syntax e AWS/VM reais pendentes.

Fontes: [GitHub OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws),
[SSH EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/connection-prereqs-general.html),
[S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3).


## Restore PR8 — implementação local preparatória

[restore.yml](restore.yml) conecta o executor ao canal SSM/SSH dedicado, com seleção
specific/latest_valid, confirmation=RESTORE e Environment. Ainda não publicado ou
ativado nesta continuação; nenhum restore real comprovado. Pré-requisitos e limites:
[PR8](../../docs/phase-7-pr8.md). Não habilitar sem role própria, destino/guards,
chave/host key e configuração administrativa autorizados. Não reutilizar deploy ou
backup role e não tratar RESTORE_CANDIDATE_READY como autorização para Destroy.
