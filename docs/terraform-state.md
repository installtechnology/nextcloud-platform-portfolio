> Registro sanitizado: ACCOUNT_ID, PUBLIC_IPV4, ADMIN_IPV4, RESOURCE_ID e lineages
> são placeholders; valores reais permanecem somente em registros privados.
> Estado atual e limites em [Fase 6](phase-6.md).

# Terraform State — backend S3 validado

Na entrega original da Fase 6, nenhum bucket havia sido criado ou state migrado.
O bootstrap do laboratório foi aplicado e seu state foi migrado posteriormente,
com autorizações separadas; a validação operacional está registrada abaixo.
Templates continuam inativos por padrão. CI gera backend.tf e exige bucket S3
existente, sem fallback local.

## Propriedade e chaves

Bootstrap cria OIDC/role GitHub, boundary EC2, policy backup preparada sem attachment
e o terceiro bucket exclusivo de state. Persistent mantém somente os dois buckets
da aplicação. Runtime é descartável. Isso substitui a proposta anterior de state
em persistent e elimina circularidade: bootstrap nasce local e depois migra seu
próprio state para S3. Nenhuma dependência de outputs/state runtime.

State bucket: privado, todos os bloqueios públicos, BucketOwnerEnforced, SSE-S3,
versionamento, TLS obrigatório, force_destroy=false, prevent_destroy e sem
lifecycle/expiração. Nunca pertence ao destroy operacional, cujo único alvo futuro
é runtime. State pode conter dados sensíveis e não é backup da aplicação.

Somente workspace default, três chaves fixas:

```text
states/<account-id>/<project>/<environment>/bootstrap/terraform.tfstate
states/<account-id>/<project>/<environment>/persistent/terraform.tfstate
states/<account-id>/<project>/<environment>/runtime/terraform.tfstate
```

Conta/região/projeto/ambiente parametrizados, account ID conferido por STS/action/
provider/backend. Cada cliente/conta tem bootstrap/role/state bucket/tfvars próprios.
Não criar chave por execução/backup. Futuros deploy/destroy/DR usam a mesma chave
runtime e concurrency por conta/projeto/ambiente, sem cancelar operações em curso.

## Locking e IAM

Terraform >=1.10; workflow fixa 1.14.2. Templates e configs geradas usam encrypt=true,
use_lockfile=true e lock <key>.tflock. Sem DynamoDB/KMS dedicada. Não usar
-lock=false, force-unlock automático ou S3 Object Lock como substituto.

GitHub: ListBucket somente com prefixos exatos persistent/runtime, GetBucketLocation;
GetObject/PutObject nos dois states; GetObject/PutObject/DeleteObject somente nos
locks. Sem DeleteObject do state, DeleteObjectVersion ou state bootstrap. SSO
administra bootstrap. EC2 não acessa state/backups. Terraform 1.14.2 tenta listar
env:/ na descoberta de workspaces: esse prefixo é negado e a versão fixada continua
com default, podendo emitir warning. Não alterar workspace_key_prefix/workspaces.

Fontes: [S3/permissões/locking](https://developer.hashicorp.com/terraform/language/backend/s3),
[fallback default na versão fixada](https://github.com/hashicorp/terraform/blob/v1.14.2/internal/backend/remote-state/s3/backend_state.go).

## Bootstrap inicial — referência para novos ambientes

Os comandos abaixo exigem autorização posterior própria, controlador Linux e
sessão administrativa SSO/federada. Não usar credenciais permanentes.

**A — preparar bootstrap com state local.** Backend.tf deve estar ausente.

```bash
aws sso login --profile "$AWS_PROFILE"
aws sts get-caller-identity
cp environments/lab/bootstrap.tfvars.example environments/lab/bootstrap.tfvars
# Editar conta/região/projeto/ambiente, três buckets e OIDC owner/repo/environment.
terraform -chdir=terraform/aws/bootstrap init -input=false
terraform -chdir=terraform/aws/bootstrap validate
terraform -chdir=terraform/aws/bootstrap plan \
  -var-file=../../../environments/lab/bootstrap.tfvars
```

Confirmar sub clássico ou imutável (owner_id/repository_id juntos). Environment
GitHub igual ao Terraform; branches restringidas no GitHub. Se provider GitHub já
existe, informar existing_provider_arn da mesma conta. Não recriar issuer compartilhado.

**B — após autorização e revisão, criar bootstrap.**

```bash
# Referência para novo ambiente: exige autorização própria; não repetir no lab migrado:
terraform -chdir=terraform/aws/bootstrap apply \
  -var-file=../../../environments/lab/bootstrap.tfvars
terraform -chdir=terraform/aws/bootstrap output
```

Outputs: github_role_arn/github_subject/ec2_permissions_boundary_arn/state_bucket_name/
state_prefix. Proteger state local e backup fora de Git/artifacts/runner/EC2.
Bucket/provider já existentes exigem referência/import e revisão administrativa.

**C — somente após bucket existir e autorização de migração.**

```bash
cp environments/backend.tfbackend.example environments/lab/bootstrap.tfbackend
# Substituir placeholders por conta/bucket/região e key bootstrap conferidos.
cp terraform/aws/bootstrap/backend.tf.example terraform/aws/bootstrap/backend.tf
terraform -chdir=terraform/aws/bootstrap init -migrate-state \
  -backend-config=../../../environments/lab/bootstrap.tfbackend
```

Conferir lineage/serial/recursos do state remoto, versionamento/locking e manter
cópia segura durante migração. Não usar -force-copy para silenciar confirmação,
-reconfigure para substituir migração, ou imprimir state em logs. Desativar cópia
local operacional após conferir. Esse roteiro descreve um novo ambiente; a migração deste laboratório já foi concluída.

**D — persistent/runtime novos começam remotos.** Criar um .tfbackend por root
com o mesmo bucket/conta/região, key correspondente; copiar backend.tf.example
para backend.tf e executar init -backend-config=... antes de qualquer operação.
Se nunca houve apply, não há state local a migrar. Havendo state local existente,
migrar administrativamente como em C; não abrir chave vazia paralela.

CI gera esses dois configs e ativa templates, init -input=false -lockfile=readonly,
plan -lock-timeout=5m. Não inicializa/aplica bootstrap. Sessão OIDC no ambiente,
sem credentials em .tfbackend/CLI/tfvars. Planos/configs temporários privados não
são artifacts. .terraform/ e backend.tf gerado são ignorados no Git.

SSO é recuperação administrativa se OIDC falhar. Restaurar state histórico exige
reconciliação com AWS real; não equivale a DR da aplicação. Versions/requests S3
acumulam custo sem expiração. O roteiro original não provisionou buckets;
o bootstrap lab foi aplicado e validado posteriormente, conforme o registro abaixo.

## Validação operacional do bootstrap lab — 2026-10-07

O bootstrap administrativo foi aplicado com 11 recursos. Seu backend S3 está
ativo na conta `ACCOUNT_ID`, região `us-east-1`, com `encrypt=true` e
`use_lockfile=true`, no objeto:

```text
s3://nextcloud-platform-state-ACCOUNT_ID-lab/states/ACCOUNT_ID/nextcloud-platform/lab/bootstrap/terraform.tfstate
```

O bucket mantém versionamento habilitado; uma versão do objeto de state foi
confirmada na validação. Durante a migração, os metadados passaram de:

| Metadado | Antes da migração | Remoto validado |
| --- | --- | --- |
| Lineage | `LINEAGE_ANTERIOR` | `LINEAGE_CANONICO` |
| Serial | `15` | `1` |

O lineage remoto **LINEAGE_CANONICO é o canônico** para
operações futuras deste bootstrap. O serial 1 é o valor nesta validação; deve
evoluir naturalmente nas operações Terraform, sem edição manual.

A comparação estrutural confirmou todos os 11 registros administrativos,
incluindo instances, IDs, attributes, provider addresses, dependencies e as
definições completas dos sete outputs. Foram ignorados somente lineage, serial
e terraform_version. A ordem da coleção check_results foi normalizada por
endereço de configuração, comparando todos os campos e valores: os mesmos
sete resultados pass estavam presentes nos dois states. Não houve diferença
em registros de recursos ou outputs. Na saída JSON do CLI, sensitive=false é
explícito; no arquivo de state esse campo pode ser omitido quando falso.

`terraform state pull` foi direcionado a arquivos protegidos em diretório
ignorado pelo Git, sem imprimir o state. `terraform state list` e
`terraform output -json` utilizaram exclusivamente o backend S3. A listagem
contém 11 recursos gerenciados e seis data sources, que não são recursos AWS
criados adicionalmente.

Foi executado somente:

```bash
terraform -chdir=terraform/aws/bootstrap plan \
  -lock-timeout=5m \
  -var-file=../../../environments/lab/bootstrap.tfvars
```

Resultado: **0 to add, 0 to change, 0 to destroy**, com refresh real e sem
salvar plano binário. O objeto `.tflock` foi observado durante o plan e ficou
ausente após a liberação automática pelo Terraform; não houve `-lock=false`,
force-unlock ou exclusão manual do lock. O state permaneceu estruturalmente
equivalente ao backup após o plan, com lineage canônico e serial 1.

O backup pré-migração permanece íntegro, com SHA256 conferido, em
`<DIRETORIO_PRIVADO_DE_BACKUP>/bootstrap-before-s3.tfstate`.
Seu diretório restringe acesso ao usuário operacional, Administradores e SYSTEM.
O arquivo local `terraform.tfstate` vazio não foi restaurado nem sobrescrito.
Backend/config reais, snapshots e relatórios operacionais permanecem ignorados.

Nesta validação não houve apply, destroy, state push, nova migração,
reconfigure, GitHub Actions, operações persistent/runtime ou Fase 7.
O roteiro acima continua como referência para novos ambientes; não deve ser
reexecutado para migrar novamente este bootstrap já validado.
