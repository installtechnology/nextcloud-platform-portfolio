# Fase 7 — PR4: IAM e segurança preparatória do backup

Base: PR3 publicado em `3951b00`. Implementação e testes locais, **sem apply,
plan conectado, acesso AWS/EC2/S3/state ou alteração operacional**. Nenhum workflow
de backup foi criado/ativado. PR5 não iniciado. Não homologa a captura nem restore.

## Auditoria do código existente

- Bootstrap possui OIDC GitHub compartilhável, role de deploy e policy operacional,
  boundary EC2, policy backup_restore sem attachment e bucket dedicado de state.
- Deploy é manual, usa Environment, subject exato (inclui IDs imutáveis quando
  configurados), boundary EC2 e state somente persistent/runtime. Nada foi alterado
  nesse fluxo. Configuração real do GitHub não foi consultada nesta tarefa.
- Persistent mantém data/backups versionados, AES256/SSE-S3, TLS obrigatório,
  bloqueios públicos, force_destroy=false e prevent_destroy. Data usa
  BucketOwnerPreferred para ACL private Nextcloud; backups usa BucketOwnerEnforced.
- Runtime mantém Instance Profile/role específica da EC2 limitada a dados pela
  boundary; não tem acesso a backups/state. Não foi ampliada.
- A policy backup_restore existente lista backups/* e permite Put/Get/multipart,
  mas não lista versões, não lê dados/version IDs, não tem boundary própria,
  contexto OIDC ou separação de captura/restore. É genérica para este novo escopo.
  Não foi anexada, alterada ou reutilizada como concessão da role PR4.

## Opt-in e identidade

`github_backup = null` mantém os novos recursos ausentes. Opt-in administrativo
prepara apenas três recursos: `aws_iam_policy.backup_boundary[0]`,
`aws_iam_role.backup[0]` e `aws_iam_role_policy.backup[0]`. A role usa o provider
existente e o mesmo repositório/Environment definidos em github_oidc. Não cria
segundo provider, bucket, Instance Profile ou recurso runtime.

Exemplo **sintético**, não adicionar a tfvars operacionais nesta etapa:

```hcl
github_backup = {
  branch        = "main"
  workflow_name = "Backup platform (manual)"
}
```

Role `${project_name}-${environment}-backup`, sessão máxima 3600 s; boundary e
policy inline possuem o mesmo documento como teto. Sem attachment de backup_restore.
IDs owner/repository são obrigatórios para o opt-in. Audience sts.amazonaws.com,
principal Federated exato e subject Environment usam StringEquals, sem curingas.
Branch e nome do futuro workflow também são condições StringEquals **separadas**.

O subject Environment não contém automaticamente a branch. A documentação AWS
atual descreve `token.actions.githubusercontent.com:ref` e `:workflow` como
claims utilizáveis na trust (não disponíveis na sessão); o PR4 usa essa capacidade
sem customizar o subject, provider ou deploy existentes. Não usa event_name, claim
não confirmada, StringEqualsIfExists na trust ou fallback permissivo.
[Claims GitHub na AWS](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_iam-condition-keys.html).

Antes de operação, validar claims reais somente de forma sanitizada, sem imprimir
token; falta/mudança de claim deve negar autenticação. Environment deverá restringir
somente a branch autorizada e exigir revisores conforme política do operador.
Workflow futuro precisa de environment correto, name exato, workflow_dispatch,
concurrency do ambiente, id-token:write e gate manual. Não é criado neste PR.
[Proteção do Environment e subject](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).

Nome de workflow não é digest do código nem substitui proteção de main/CODEOWNERS.
Um administrador do repositório pode alterar workflow/regras; confiança e revisão
humana permanecem necessárias. Pin de reusable workflow exigiria outra proposta.

## Permissões e limites

| Escopo | Permissão e justificativa |
| --- | --- |
| EC2 na região definida | DescribeInstances/DescribeInstanceStatus, somente descoberta e estado |
| Dois buckets exatos | GetBucketLocation/GetBucketVersioning/GetEncryptionConfiguration para preflight |
| Bucket de dados | ListBucket/ListBucketVersions; inventário completo e ausências/delete markers |
| Objetos de dados | GetObject/GetObjectVersion para HEAD/verificação de versão específica |
| Bucket de backups | ListBucket/ListBucketVersions apenas prefixo backups/ ou descendentes |
| Objetos backups/* | PutObject, GetObject/GetObjectVersion, ListMultipartUploadParts, AbortMultipartUpload |

DescribeInstances/DescribeInstanceStatus não oferecem isolamento por ResourceTag
no desenho utilizado: Resource=* com aws:RequestedRegion. A role pode ler metadados
de outras EC2 na mesma conta/região. Não fingimos restrição IAM por filtros CLI.
O workflow futuro deverá filtrar/verificar Project/Environment, região, estado,
Instance Profile e unicidade, recusando zero/múltiplos alvos. Sem leitura de state.
[DescribeInstances](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeInstances.html).

ListObjectVersions retorna versões e delete markers com paginação; não há action
IAM adicional para delete markers. PR5 deverá processar KeyMarker e VersionIdMarker
até IsTruncated=false e selecionar versões/ausências conforme contrato v1.
[ListObjectVersions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectVersions.html).
Delete marker corrente representa ausência, não conteúdo para restaurar. HEAD
pode responder 404 nesse caso e 405 para VersionId de delete marker; erro 403 não
deve ser convertido silenciosamente em ausência. Cruzar resposta com inventário
e rejeitar paginação incompleta permanece responsabilidade do PR5.

HEAD usa GetObject ou GetObjectVersion: IAM não separa leitura de metadados de
leitura de conteúdo. Portanto a role pode ler conteúdo dos objetos de dados;
esse é o custo de autorizar HEAD/versionId. Não é uma role de escrita ou restore
de dados. Não usamos actions inventadas s3:HeadObject ou s3:ListObjectVersions.
[HeadObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html).

Todos os Allows S3 exigem TLS e s3:ResourceAccount esperado. Na avaliação das
policies de identidade/boundary, PutObject aceita AES256 explícito ou criptografia
padrão do bucket; headers aws:kms/SSE-C não recebem Allow;
não há kms:Decrypt/GenerateDataKey nem PutObjectAcl. SSE-S3 não substitui envelope
age do PR5. Objetos antigos SSE-KMS exigiriam decisão própria; não ampliar IAM.

Negações explícitas: state, S3 fora do escopo, mutações em dados, DeleteObject/
DeleteObjectVersion, listagem backups sem prefixo autorizado, HTTP e APIs não
listadas. Sem IAM/PassRole, AssumeRole encadeado, EC2 provisionamento/parada/
Destroy, mudanças SG, tags, SSM SendCommand, Instance Connect ou administração S3.
Boundary não concede acesso por si; exige Allow na identity policy. Tests de
interseção/deny são sintéticos, não AWS Policy Simulator. SCPs, políticas de
recurso, endpoint e demais guardrails só poderão ser avaliados com autorização.

AbortMultipartUpload remove apenas partes de upload não concluído, não objetos/
versões armazenados. ListBucketMultipartUploads não foi concedido: o futuro
produtor deve registrar UploadId e tratar interrupções, sem descobrir/apagar
uploads de outros runs. Nenhum cleanup de upload é executado neste PR.
PutObject ainda permite nova versão na mesma chave. IAM não prova unicidade de
backup_id ou impede mistura: PR5 deverá impor ID único, rejeitar colisões e
publicar completion por último. Retenção não é Object Lock/WORM.

Prefixo backups/ segue o contrato PR1; o bucket já é separado por ambiente.
Sem lifecycle/expiração, exclusão de versões, restore, autorização de Destroy ou
acesso ao state. Não protege contra perda total do bucket de dados.

## SSH: decisão ainda pendente, sem concessão automática

A role não abre/fecha regra TCP/22. Runner hospedado com IP variável não conseguirá
SSH pelo CIDR administrativo existente apenas por assumir esta role. Chave privada,
host key, usuário, journal/supervisor e recuperação de manutenção exigem desenho
e aprovação próprios. Não reaproveitar a role deploy para contornar este bloqueio.

Alternativas a revisar antes de PR6: runner dedicado com egress fixo previamente
autorizado; runner isolado na VPC com SG de origem controlado; execução local por
operador autorizado e role apenas para inventário/transferência; SSM/Instance
Connect com agent/endpoints/permissões específicos. Cada alternativa tem custos,
custódia de credenciais e superfície de ataque diferentes, sem implementação aqui.

Se futuramente escolhido SG temporário, apresentar patch separado mínimo com
authorize/revoke, tags de ownership, recurso SG/regra e /32/timeouts, cleanup seguro
e auditoria resource-level. Não conceder CreateTags ou SG write preventivamente.

## Validação local e procedimento administrativo futuro

Terraform 1.14.2/provider AWS 6.67.0 preservados. Fmt e cópias isoladas dos três
roots: **sem backend.tf, state, tfvars operacionais ou credenciais**. Init
-backend=false -lockfile=readonly usa somente mirror local já disponível, sem
download/upgrade. Validate e terraform test usam mocks; command=plan nos testes
não é plan conectado. Dados da policy/trust são JSON real calculado por Terraform.

`test_backup_iam.py` lê o JSON verbose desse teste e verifica actions/resources/
conditions/trust/boundary, positivos e negativos. O pequeno avaliador cobre apenas
os operadores utilizados e rejeita operador desconhecido; não reproduz IAM inteiro.
Não utiliza cópia de policy escrita manualmente como substituto da implementação.

```text
terraform -chdir=COPIA_BOOTSTRAP_OFFLINE test -json -verbose
# Redirecionar saída JSON para arquivo local ignorado e definir BACKUP_IAM_TEST_PLAN.
python -m unittest discover -s terraform/aws/bootstrap/tests -p test_backup_iam.py -v
```

Futuro apply **administrativo, autorização separada**: preservar state canônico,
conferir conta/região/OIDC e políticas realmente aplicadas; configurar opt-in
deliberadamente; gerar plan bootstrap com tfvars autorizados; comparar cada
statement e exigir somente os três recursos novos e outputs esperados. Abortá-lo
se houver mudanças em deploy/EC2/OIDC/buckets/boundaries existentes. Só após revisão
e autorização explícita aplicar o plano salvo, validar IAM e posterior plan zero.
Não usar -target, mudar state ou ampliar permissões para contornar uma falha.
Não habilitar workflow/backup durante esse apply.

Este PR mantém opt-in null nos inputs existentes: não houve plan real para alegar
impacto 0/0/0. Com opt-in, expectativa é 3 add; avaliação futura pode revelar drift
ou output novo, devendo parar para revisão. IAM não tem custo direto previsto;
S3/runner/backup terão custos apenas quando operações posteriores forem autorizadas.

Os 2 skips POSIX PR2 e 3 skips POSIX PR3, PostgreSQL/Nextcloud reais, SIGKILL,
reboot, escritores externos, claims OIDC e autorização AWS permanecem bloqueadores.
Não há prova de backup consistente ou restauração funcional neste PR4.

## Resultado local e limites remanescentes

Windows, Terraform 1.14.2/AWS 6.67.0: init sem backend, validate e testes dos três
roots aprovados. Bootstrap: 19 runs (6 existentes, 13 PR4); persistent: 2;
runtime: 5. Todos com mock e command=plan. Testes semânticos adicionais: 21,
incluindo decisões positivas/negativas parametrizadas e claims ausentes, branch,
tag/PR e subject Environment incompatíveis. Policy máxima: 4402 caracteres,
abaixo do limite managed de 6144; provider/lockfiles não alterados.

Regressões offline: policy deploy HCL/limite (19 statements, 8734 caracteres),
controlador 13; contratos 73; PR2 187 aprovados/2 skips POSIX; PR3 71/3 skips
POSIX. Nenhum resultado significa AWS Access Analyzer, IAM Simulator, AssumeRole
real ou sucesso de APIs S3/EC2. Não houve instalação/download de dependências.

Role/boundary têm prevent_destroy. Retirar o opt-in depois de um apply autorizado
não é procedimento de rollback automático: deverá apresentar outro plan e decisão
administrativa. Inputs atuais não foram alterados. Credenciais futuras expiram em
no máximo uma hora; PR6 precisará avaliar timeout/renovação e interrupções sem
abandonar uploads ou manutenção. Custódia de chave age/SSH e fencing externo
permanecem decisões pendentes; não são resolvidos pela IAM.

## Auditoria final de segurança e compatibilidade

Foram revisados os sete arquivos PR4, inclusive os quatro untracked. Uma falha
concreta foi corrigida: a validação aceitava `main/.hidden`, rejeitado por
`git check-ref-format --branch`. Componentes iniciados por ponto agora são
rejeitados, com teste de regressão. Nenhuma action ou concessão IAM foi ampliada.

### Identidade OIDC e evidência local

Issuer é vinculado pelo principal Federated ao provider
`token.actions.githubusercontent.com`, cujo URL e audience são definidos no
bootstrap existente. Não existe principal AWS ou federated curinga. `sub` exige
Environment exato; `ref` exige branch separadamente; IDs de repository/owner,
audience e nome do workflow exigem igualdade simultânea. Não há fallback para
claims ausentes. `environment` não foi adicionado como condição redundante:
já está no subject exato. Claims de trust não são filtros disponíveis nas APIs
S3/EC2 da sessão. AssumeRole real do futuro workflow permanece não validado.

O formato antigo é `repo:OWNER/REPO:environment:ENV`; o imutável inclui
`OWNER@OWNER-ID/REPO@REPO-ID`. A documentação oficial descreve a adoção do formato
imutável por novos repositórios e opt-in dos anteriores, sem autorizar inferência
apenas pelo nome. Registros locais preservados da validação anterior do projeto
indicam formato imutável; seu prefixo/IDs foram comparados à configuração local,
sem publicar valores. Não consultamos GitHub/AWS nem renovamos essa evidência.
[Formatos de subject do GitHub](https://docs.github.com/en/actions/reference/security/oidc).

Fixtures verificam o formato antigo com backup desabilitado e a rejeição desse
subject pela nova trust imutável. Assim, `github_backup=null` preserva deploy
legado; habilitar backup exige IDs e evidência do formato efetivamente emitido.
Repositórios legados que ainda emitem formato antigo exigem decisão administrativa
antes de qualquer mudança de subject/configuração; o PR4 não a executa.

`workflow` verifica **nome**, não caminho, commit ou assinatura do arquivo. Dois
arquivos com o mesmo nome e claims idênticas são indistinguíveis à trust atual;
há teste que explicita esse limite. Antes de habilitar o workflow futuro:

- Restringir deployments do Environment à branch autorizada; revisar também
  regras de main e permissões de escrita/dispatch.
- Exigir revisão dos arquivos de workflow/IAM com CODEOWNERS e proteção de branch;
  impedir nomes duplicados durante revisão do futuro workflow.
- Configurar revisores do Environment, prevenção de autoaprovação e bloqueio de
  bypass administrativo quando disponíveis no plano do GitHub; verificar os
  controles efetivos, sem presumir que estejam habilitados.
- Emitir `id-token:write` somente no job aprovado e não executar código de PR
  não confiável nesse job. Environment/branch não substituem revisão do código.

### Avaliação conjunta das policies

As bucket policies presentes no código persistent contêm apenas Deny para HTTP,
sem Allow, NotPrincipal ou concessão direta à sessão. Nesse desenho, os Allows
resultam da interseção identity/boundary, sujeita aos Denies de todas as policies.
Resource ARN de bucket é usado para metadados/listagem; ARN de objeto para
Get/GetVersion/Put/Abort/Parts. Testes negativos rejeitam intercâmbio desses ARNs.
Não foi verificada a ausência de drift nas policies remotas.

State, exclusão de objetos/versões, escrita em dados, APIs fora da lista, acesso
fora dos ARNs e listagem fora do prefixo têm Deny **explícito**. Região incorreta
na descoberta, owner incorreto/ausente e headers de criptografia não autorizados
dependem de ausência de Allow (**negação implícita**). Não são garantias globais
equivalentes aos Denies explícitos.

Uma futura resource policy com Allow direto à sessão, na mesma conta, pode
superar negações implícitas da boundary/identity; Deny explícito continua
prevalecendo. O ensaio sintético demonstra isso para SSE-C e separadamente os
Denies de state/dados/delete. Não aplicamos essa hipótese ao ambiente nem
acrescentamos permissões/guardrails preventivos: tais Allows não existem nas
bucket policies examinadas. Qualquer alteração futura de resource policy exige
nova avaliação conjunta. Boundary não é uma garantia de imutabilidade universal.
[Avaliação de boundaries e resource policies](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html).

Necessidade das actions: DescribeInstanceStatus complementa DescribeInstances
com status checks no preflight futuro; ambos expõem descoberta regional, sem
isolamento por tags. GetBucketLocation/Versioning/EncryptionConfiguration verificam
pré-requisitos; ListBucket/ListBucketVersions permitem inventário/paginação e
delete markers; GetObject/GetObjectVersion permitem HEAD/GET corrente/específico.
PutObject cobre PUT e início/partes/conclusão de multipart no modelo S3;
ListMultipartUploadParts verifica partes de UploadId conhecido e AbortMultipartUpload
permite encerrar somente esse upload incompleto. Não há ListBucketMultipartUploads,
Delete, ACL, KMS, restauração, SG write ou execução remota concedidos. As fases
futuras deverão demonstrar a integração concreta; testes IAM não executam APIs.

### Dependências, mudanças futuras e aceite

Os três recursos compartilham opt-in, boundary é obrigatória e a policy inline
depende da role. Provider OIDC é reutilizado. Nomes são distintos das roles/policies
existentes e respeitam limites de tamanho; conflitos com IAM pré-existente fora
do state não foram consultados. Não importar/remover recursos para contorná-los.
Mudança de project/environment/nome pode exigir substituição bloqueada por
prevent_destroy; retirar opt-in também não destrói automaticamente. Mudanças de
branch/workflow/IDs podem bloquear autenticação e precisam de revisão do plan.
Atualizações de boundary/inline não são transação atômica: validar ambas antes
de liberar operação. Outputs null e ausência dos três recursos foram testados
sem opt-in, incluindo formato legado. Nenhuma configuração real habilita backup.

Validação final Windows: bootstrap **19 runs**, persistent **2**, runtime **5**
com mocks e sem backend; testes semânticos **21**. Regressões: contratos **73**,
PR2 **187 aprovados/2 skips POSIX**, PR3 **71/3 skips POSIX**, controlador **13**.
Fmt, validate dos três roots, parser HCL/Python, policy deploy, scan de secrets e
diff-check incluindo untracked foram reexecutados. Não substituem validação AWS,
claims emitidas, SCPs/session policies/endpoints ou homologação Linux/captura.
Os skips POSIX e os ensaios PostgreSQL/Nextcloud/SIGKILL/reboot/escritores externos
continuam bloqueadores do uso operacional. Sem comprovação de restauração ou
qualquer autorização de Destroy.

Regressões adicionais: storage guard **17** (nove configs oficiais já disponíveis
localmente) e instalação inicial **18**, com PHP local e adapters sintéticos.
Check estático do controlador passou em cópia isolada dos arquivos versionados.
A execução direta desse check no checkout original encontra o backend.tf local
ignorado, cuja ausência ele exige em um checkout de código; esse arquivo ativo
foi preservado, sem alteração do teste ou da configuração operacional. Nenhum
teste de containers reais foi executado nesta auditoria.
