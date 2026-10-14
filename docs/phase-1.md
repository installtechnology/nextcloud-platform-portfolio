> Registro histórico. A separação de buckets/IAM e a configuração de storage foram revisadas na [Fase 4](phase-4.md); seguir a documentação atual para instalação nova.

# Fase 1 — revisão arquitetural de 2026-10-07

> Relatório da Fase 1. Default EC2 atualizado por autorização antes da Fase 2;
> implementação e validações posteriores estão em [phase-2.md](phase-2.md).

Fase 1 concluída e revisada antes de iniciar Fase 2. Nenhum apply/destroy,
migração de state ou recurso AWS executado/criado. A entrega inicial tinha dois
roots; esta revisão corrige a dependência administrativa de runtime.

## Nova árvore (cache .terraform omitido)

```text
nextcloud-platform/
├── .gitignore
├── AGENTS.md
├── README.md
├── .github/workflows/README.md
├── terraform/
│   ├── aws/
│   │   ├── bootstrap/
│   │   │   ├── .terraform.lock.hcl
│   │   │   ├── iam.tf
│   │   │   ├── main.tf
│   │   │   ├── outputs.tf
│   │   │   ├── variables.tf
│   │   │   ├── versions.tf
│   │   │   └── tests/base.tftest.hcl
│   │   ├── persistent/
│   │   │   ├── .terraform.lock.hcl
│   │   │   ├── main.tf
│   │   │   ├── outputs.tf
│   │   │   ├── variables.tf
│   │   │   ├── versions.tf
│   │   │   └── tests/base.tftest.hcl
│   │   └── runtime/
│   │       ├── .terraform.lock.hcl
│   │       ├── iam.tf
│   │       ├── main.tf
│   │       ├── outputs.tf
│   │       ├── variables.tf
│   │       ├── versions.tf
│   │       └── tests/base.tftest.hcl
│   └── azure/README.md
├── environments/
│   ├── lab/{bootstrap,persistent,runtime}.tfvars.example
│   └── customer-template/{bootstrap,persistent,runtime}.tfvars.example
├── ansible/
│   ├── inventory/README.md
│   ├── roles/README.md
│   └── playbooks/README.md
├── docker/README.md
├── monitoring/README.md
├── scripts/README.md
└── docs/
    ├── decisions.md
    ├── phase-1.md
    └── terraform-state.md
```

## Arquivos criados nesta revisão

- terraform/aws/bootstrap/: main.tf, iam.tf, variables.tf, outputs.tf,
  versions.tf, .terraform.lock.hcl e tests/base.tftest.hcl.
- environments/lab/bootstrap.tfvars.example.
- environments/customer-template/bootstrap.tfvars.example.
- docs/terraform-state.md.

## Arquivos alterados nesta revisão

- terraform/aws/runtime/iam.tf: removidos provider/role/policy GitHub.
- terraform/aws/runtime/variables.tf: removida variável github_oidc.
- terraform/aws/runtime/outputs.tf: removido output da role GitHub.
- terraform/aws/runtime/tests/base.tftest.hcl: testes OIDC transferidos a bootstrap.
- environments/lab/runtime.tfvars.example e
  environments/customer-template/runtime.tfvars.example: OIDC removido.
- README.md, docs/decisions.md, docs/phase-1.md: arquitetura/sizing/state revisados.
- AGENTS.md: regra de preservar bootstrap/persistent e backend futuro.
- .gitignore: ignorar configurações locais reais *.tfbackend.

Persistent não teve recursos modificados. Runtime mantém t3.medium/gp3 12 GiB,
instance_type parametrizado; default atualizado a pedido do operador antes da Fase 2; nenhuma VM real alterada.

## Responsabilidade e preservação

| Root | Recursos hoje | Destino no destroy operacional futuro |
| --- | --- | --- |
| bootstrap | OIDC GitHub opcional, role GitHub, trust e policy inicial de leitura de backups | Preservar |
| persistent | S3 de dados, versionamento, criptografia, controle de ownership/acesso público e TLS | Preservar |
| runtime | VPC/subnet/IGW/rotas/SG, chave pública SSH, EC2/EBS, IAM exclusivo da EC2 | Remover |

Bootstrap não depende de runtime, de terraform_remote_state ou de bucket S3
existente; usa ARN composto com a partição atual e nome configurado. Provider
existente do cliente pode ser referenciado. Provider criado e role administrativos
receberam prevent_destroy. Ao remover runtime, autenticação OIDC permanece.

Futuro bucket dedicado de state será gerenciado por persistent e permanecerá
junto dos dados/IAM administrativos. Não foi adicionado nesta revisão. Sem
bloco backend remoto ativo. O requisito inicial de sobreviver apenas um S3 foi
explicitamente atualizado pela revisão solicitada.

## Estratégia de state

Documentada em terraform-state.md: S3 dedicado versionado/criptografado, chave
por conta/projeto/ambiente/root, workspace default, locking nativo
use_lockfile=true e Terraform >=1.10. Sem DynamoDB. Deploy/destroy/DR usarão
exatamente o mesmo backend/key runtime. States bootstrap/persistent separados
e administrativos; EC2 não tem acesso ao state.

Bucket precisa existir antes do backend: primeira criação futura sob SSO/sessão
federada com state local protegido, depois migração autorizada por root.
Não executados init -migrate-state, import, state mv ou configuração remota.
Nenhum state local operacional existente foi encontrado nesta revisão.

## Comandos executados e resultados

- Leitura de AGENTS.md e arquivos Terraform/documentação existentes.
- Consulta à documentação primária HashiCorp sobre backend S3 e configuração parcial.
- terraform fmt -recursive: passou.
- terraform fmt -check -recursive: passou.
- terraform init -backend=false -input=false -lockfile=readonly em bootstrap,
  persistent e runtime: passou nos três.
- terraform validate -no-color em bootstrap, persistent e runtime:
  configuração válida nos três.
- terraform test -no-color em bootstrap: 4 passaram, 0 falharam.
- terraform test -no-color em persistent: 1 passou, 0 falharam.
- terraform test -no-color em runtime: 2 passaram, 0 falharam.
- rg --files --hidden excluindo cache/state: inventário de arquivos.
- rg de github/oidc/bootstrap/backend em runtime: nenhuma referência.
- Inspeção de README/IAM e busca de arquivos tfstate/tfplan: nenhum encontrado.

Ambiente de validação: Terraform 1.14.2 Windows, hashicorp/aws 6.67.0 fixado nos
três lockfiles. Init/validate/test executados com autorização de download/plugin
local fora das restrições de rede/comunicação do sandbox, sem provisionamento.

Testes são provider mock com command=plan. Bootstrap cobre opt-in, desabilitado,
provider existente e rejeição de wildcard; persistent cobre controles de storage;
runtime cobre defaults e rejeição de SSH público. IAM policy_document mockado:
não comprova permissões efetivas AWS. Nenhum teste real de rede, aplicação ou
DR. Test teardown citado pelo Terraform limpa contexto simulado; não é destroy AWS.

## Custos, riscos e pendências

Nenhum custo cloud iniciado. Infraestrutura futura cobra EC2/EBS/IPv4/S3/tráfego.
Backend futuro adiciona pequenos states/versões/requests S3, sem tabela DynamoDB,
KMS dedicada ou servidor adicional; versionamento sem expiração acumula custos.

Role GitHub ainda possui somente leitura de backups. IAM mínimo de runtime/
backend e workflows operacionais serão implementados na Fase 6. Não há destroy
workflow nesta revisão. Seu único alvo futuro será runtime e a trava de backup
completo continua obrigatória na Fase 7.

Sizing final exige medição de Nextcloud, PostgreSQL, Redis, Prometheus, Grafana,
Loki e Alloy, incluindo concorrência, retenção/logs e picos de backup/restore.
t3.medium é somente default inicial parametrizado, não sizing certificado.

Caso algum operador tenha aplicado a versão anterior fora deste workspace,
mover IAM entre states exige migração/import administrativo revisado antes de
aplicar; não aplicar a remoção antiga diretamente. Neste workspace só arquivos
foram separados, porque não houve apply.

Ainda pendentes: aprovação para provisionar, parâmetros reais por conta, backend
implementado/criado/migrado e validação operacional futura. Recuperação histórica
de objetos S3 também exige definição nas fases específicas. Fase 2 não iniciada.