# Fase 4 — S3 Primary Object Storage e buckets independentes

Implementação para instalação **nova**, sem infraestrutura previamente aplicada.
Somente configuração e validações locais foram executadas. Não houve apply,
destroy, criação de infraestrutura AWS, execução de containers ou migração.
Observabilidade, workflows, backup e DR não foram implementados. Default de
instance_type permanece parametrizado e inalterado; sizing final continua pendente.

## Arquitetura e ciclo de vida

| Camada | Recursos e identidade | Após destroy operacional futuro |
| --- | --- | --- |
| bootstrap | OIDC/role GitHub opcionais, leitura de backups e política backup_restore preparada sem attachment | Preservados |
| persistent / nextcloud-data | Bucket exclusivo para conteúdos do Nextcloud; nome global único em data_bucket_name | Preservado |
| persistent / nextcloud-backups | Bucket exclusivo para PostgreSQL/config/metadata de backups manuais futuros; nome em backups_bucket_name | Preservado |
| runtime | VPC/subnet/IGW/rotas/SG, EC2/EBS, chave pública e IAM Profile de aplicação | Único alvo |
| Futuro Terraform state | Terceiro bucket administrativo independente, ainda não implementado | Será preservado |

Os dois buckets usam prevent_destroy, force_destroy=false, versionamento Enabled,
criptografia AES256/SSE-S3, todos os bloqueios públicos e
bucket policy que nega transporte não TLS. Não existe lifecycle de objetos nem
expiração automática. Validation impede reutilizar o mesmo nome para ambos.
Nomes lógicos nextcloud-data/nextcloud-backups recebem sufixos únicos definidos pelo
cliente; não são prefixes dentro de um único bucket. Nenhum state ou objeto a migrar.
prevent_destroy protege enquanto o bloco está na configuração; não substitui IAM.

Dados usam BucketOwnerPreferred: o driver **v34.0.4** envia ACL private em PUT/copy,
incompatível com BucketOwnerEnforced. Todos os bloqueios públicos continuam ativos;
role EC2 recebe PutObjectAcl somente com s3:x-amz-acl=private, sem permitir grants
públicos ou ACLs arbitrárias. Bucket/role pertencem à mesma conta. Backups mantêm
BucketOwnerEnforced. Não foi necessário alterar/patchear o driver oficial.

## IAM e credenciais

Role EC2: GetBucketLocation/ListBucket apenas no bucket de dados; GetObject,
PutObject, DeleteObject, AbortMultipartUpload e ListMultipartUploadParts apenas em
seus objetos, mais PutObjectAcl condicionado a private. HeadBucket usa ListBucket.
Sem prefixes artificiais: o Nextcloud usa chaves internas como urn:oid:<id>.
Sem acesso a backups/state, CreateBucket, ACLs públicas/arbitrárias,
DeleteBucket ou DeleteObjectVersion. DeleteObject em bucket versionado mantém
histórico/delete markers; não há limpeza automática de versões.

Bootstrap compõe ARN apenas do bucket de backups sem consultar runtime ou S3.
GitHub mantém leitura limitada ao prefixo backups/. A nova policy backup_restore
prepara leitura/escrita/multipart em backups/* e listagem limitada a backups/;
não permite excluir objetos/versões e **não está anexada a nenhuma identidade**.
A futura identidade de backup/restore será definida/vinculada na fase autorizada.
O instance profile da aplicação não recebe essa policy. Nenhuma rotina de backup
ou restore foi criada ou executada.

Nextcloud não recebe Access Key, Secret Key, credenciais OIDC do controlador ou
arquivos ~/.aws. O driver stable34, fixado pela imagem da Fase 3, usa o default
provider do AWS SDK sem shared config files quando key/secret são omitidos.
Assim obtém e renova credenciais temporárias do Instance Profile da EC2 via IMDSv2.
Tokens obrigatórios e hop limit 2 já permitem tráfego de bridge Docker. Containers
que alcançam IMDS compartilham essa identidade; não há isolamento IAM por processo.
Conectividade com IMDS e HTTPS regional S3 precisa ser comprovada em VM.

## Configuração e instalação futura autorizada

Entradas obrigatórias adicionais no controlador Ansible:

| Entrada | Origem futura |
| --- | --- |
| NEXTCLOUD_S3_BUCKET / nextcloud_stack_s3_bucket | persistent output data_bucket_name; também runtime.data_bucket_name |
| NEXTCLOUD_S3_REGION / nextcloud_stack_s3_region | persistent output bucket_region, igual à região runtime |
| PLATFORM_HOST | runtime output public_ip, sem IP hardcoded |

Persistent tfvars recebe ambos os nomes; runtime tfvars recebe somente data_bucket_name;
bootstrap tfvars recebe somente backups_bucket_name. Usar exemplos lab/customer-template,
substituir placeholders e conferir outputs quando houver provisioning autorizado.
Chave SSH privada permanece externa, com host key verificada, como na Fase 2.
As quatro senhas e demais entradas da Fase 3 continuam obrigatórias e estáveis.

platform.config.php configura OC\Files\ObjectStore\S3 com bucket/região explícitos,
HTTPS e virtual host regional AWS padrão. O template atual assume amazonaws.com;
partições/endpoints alternativos exigem revisão antes de implantação.
verify_bucket_exists=false evita o ramo stable34 que tenta criar bucket ausente;
Terraform cria o bucket e a role não permite CreateBucket. Não há fallback local.
O bucket de dados deve ser exclusivo, existente, vazio para a instalação inicial
e na mesma região da EC2. Não montar config/ antes de a imagem copiar defaults.

O fluxo inicial usava pre-installation. Foi substituído na revisão operacional por
[before-starting com instalador seguro](nextcloud-initial-installation.md), para
retomar cópias interrompidas e manter senhas fora de argv. A configuração S3 continua **antes** de maintenance:install;
before-starting verifica e sincroniza configuração em reexecuções. storage-guard.php
registra config/.platform-storage.json com identidade não secreta e recusa:

- instalação local já marcada como installed;
- instalação existente sem marcador desta plataforma;
- alteração de bucket, região, classe ou opções;
- metadados S3 inválidos.

Não apagar o marcador nem remover volumes para contornar esses checks. Nenhuma
cópia/migração de uploads locais está implementada. Reinícios compatíveis não
reescrevem o marcador; a cópia de config ocorre somente quando diferente. Os módulos
Compose preservam volumes/recreate auto; idempotência real ainda exige duas execuções.

Probe de integração PHP verifica configuração efetiva e HeadBucket autenticado,
além de PostgreSQL/Redis/cache já existentes. Não escreve/exclui objetos. Sua falha
faz a validação Ansible falhar; não converte storage nem recria bucket. Healthcheck
HTTP isolado não comprova S3. Upload/download são testes futuros em host autorizado.

## Arquivos criados/alterados

```text
nextcloud-platform/
├── AGENTS.md
├── README.md
├── terraform/aws/
│   ├── bootstrap/{main,variables,outputs,iam}.tf
│   │   └── tests/base.tftest.hcl
│   ├── persistent/{main,variables,outputs}.tf
│   │   └── tests/base.tftest.hcl
│   └── runtime/{main,variables,outputs,iam}.tf
│       └── tests/base.tftest.hcl
├── environments/{lab,customer-template}/
│   └── {bootstrap,persistent,runtime}.tfvars.example
├── ansible/
│   ├── README.md
│   ├── roles/nextcloud_stack/
│   │   ├── defaults/main.yml
│   │   ├── tasks/main.yml
│   │   └── templates/env.j2
│   └── tests/check_static.py
├── docker/
│   ├── README.md
│   ├── .env.example
│   ├── compose.yml
│   ├── platform.config.php
│   ├── configure-nextcloud.sh
│   ├── storage-guard.php              # novo
│   ├── nextcloud-integration.php
│   └── tests/
│       ├── check_static.py
│       └── check_storage.py           # novo
└── docs/
    ├── decisions.md
    ├── terraform-state.md
    ├── phase-1.md                     # aviso de revisão histórica
    ├── phase-3.md                     # aviso de revisão histórica
    └── phase-4.md                     # novo
```

## Validações e comandos executados

Windows/PowerShell; ferramentas portáteis/fixtures locais ignoradas pelo Git.
Comandos relativos à raiz E:\NextCloud\nextcloud-platform:

```powershell
terraform fmt -recursive
terraform fmt -check -recursive
# Em cada root bootstrap, persistent e runtime:
terraform -chdir=terraform/aws/<root> init -backend=false -input=false
terraform -chdir=terraform/aws/<root> validate
terraform -chdir=terraform/aws/<root> test

$env:PYTHONPATH = Join-Path (Get-Location) '.validation-libs'
python ansible/tests/check_static.py
python docker/tests/check_static.py --docker-cli .validation-tools/docker.exe --docker-config .validation-tools/config
python docker/tests/check_storage.py .validation-tools/php/php.exe
# Para os quatro arquivos PHP em docker/:
.validation-tools/php/php.exe -n -l <arquivo.php>
# Para configure-nextcloud.sh, postgres-init.sh e redis-entrypoint.sh:
& 'C:\Program Files\Git\bin\bash.exe' -n <arquivo.sh>
Get-Command ansible-playbook,ansible-lint -ErrorAction SilentlyContinue
```

- fmt/check, init -backend=false e validate nos três roots: aprovados.
- Terraform test: providers AWS mock, todos os runs command=plan, sem conta AWS;
  bootstrap 4, persistent 2 e runtime 2 testes. Inclui recusa de nomes iguais,
  proteção dos dois buckets, versionamento/SSE/ACL/bloqueio público e profile/IMDSv2.
  Um teste SSE foi corrigido para o tipo set do provider e reexecutado.
- YAML/Jinja/contratos Ansible: 13 arquivos e 43 tasks aprovados.
- Compose config --quiet real, parâmetros S3 obrigatórios e contratos estáticos:
  aprovados, sem daemon/up/containers. IAM/TLS/retenção verificados por contratos.
- Teste PHP offline: instalação nova/rerun, recusa de local existente/marcador
  ausente/troca de bucket/região, sem credenciais ou chamadas AWS: aprovado.
- PHP lint nos quatro arquivos e shell -n nos três scripts: aprovados.
- Ansible syntax-check/lint não executados: executáveis ausentes neste controlador
  Windows; YAML/Jinja não os substituem. Comandos Linux futuros na documentação Ansible.

Terraform mock simula atributos/JSON do provider, não comprova políticas efetivas
AWS, S3, credenciais ou aplicação funcional. Nenhum apply/destroy real foi executado.

## Riscos e pendências

Conteúdo S3 sozinho não recupera a instância: metadados PostgreSQL, config/apps e
identidade de storage continuam em volumes locais descartáveis. Backup/DR coordenado
e versões históricas ficam para fases futuras. Versionamento sem expiração acumula
custo; não há garantia PITR pela simples retenção de objetos.

Pendentes em VM autorizada: Ansible syntax-check/lint em Linux, teste IAM/IMDSv2 e
renovação de credenciais, instalação, upload/download, objetos no bucket correto,
uploads simples/multipart e cópias com ACL private,
ausência de writes em backups, restart e duas execuções idempotentes. Nenhum sizing
final medido; HTTP de laboratório e demais limites anteriores permanecem.

**Parar ao final da Fase 4.** Não iniciar observabilidade, workflows, backup ou DR.

Fontes primárias: [Nextcloud Primary Object Storage](https://docs.nextcloud.com/server/latest/admin_manual/configuration_files/primary_storage.html),
[driver S3 stable34](https://github.com/nextcloud/server/blob/stable34/lib/private/Files/ObjectStore/S3ConnectionTrait.php),
[hooks da imagem 34](https://github.com/nextcloud/docker/blob/master/34/apache/entrypoint.sh).

Compatibilidade ACL: [driver exato v34.0.4](https://github.com/nextcloud/server/blob/v34.0.4/lib/private/Files/ObjectStore/S3ObjectTrait.php),
[AWS Object Ownership](https://docs.aws.amazon.com/AmazonS3/latest/userguide/about-object-ownership.html),
[PutObject e permissões ACL](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html).
