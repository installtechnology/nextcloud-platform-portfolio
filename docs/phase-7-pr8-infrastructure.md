# PR8 — destino independente e IAM (preparação local)

Estado: não aplicado, não publicado, sem restauração funcional comprovada.
O patch de restore e canal foi qualificado no run 38381399266, commit
`e36ab37079c3490fde15ebb935c6092e3644804e`. Este Terraform é uma alteração nova.

## Escopo e isolamento

Bootstrap mantém `github_restore = null` como padrão. O opt-in contém
`destination_environment`, `destination_bucket_name` e `target_instance_id`
opcional. Sem ID cria somente a boundary EC2 do destino; com ID exato acrescenta
boundary, role e policy inline exclusivas do restore. Não modifica a role de
backup, aplicação original, deploy, provider OIDC ou autoridade da policy original.

O root `terraform/aws/restore` usa state remoto novo. Não reutilizar keys de
bootstrap, persistent ou runtime. O backend.example não é ativo; init remoto e
plan real dependem de configuração privada revisada e autorização. Não há
terraform_remote_state, import, removed, forget ou alteração do state original.

A rede/EC2/IAM de aplicação do destino reproduzem o runtime existente, nos arquivos
runtime.tf e iam.tf, sem editar o root operacional. Uma cópia explícita evita a
configuração de provider embutida do root antigo e permite mocks reais no novo
root. Testes devem verificar que esses arquivos continuam equivalentes ao runtime.
Sem NAT Gateway, RDS, EKS, Elastic IP ou load balancer. T3 medium, gp3 criptografado,
IMDSv2 required/hop2, CIDRs administrativos obrigatórios, SSM mínimo. O EBS do
novo destino é descartável na terminação, como no runtime; nenhuma terminação
está autorizada. Containers não são criados pelo Terraform.

O bucket NOVO é protegido por prevent_destroy, force_destroy=false, versionamento,
SSE-S3 AES256, bloqueio público, BucketOwnerPreferred e Deny de HTTP. Não cria outro
bucket de backups. A policy deste destino é gerenciada apenas por este root:
não possui fencing de backup nem compartilha a policy administrativa da origem.
Não apontar o coordenador da origem para o destino. Uma futura promoção como
ambiente de backup precisará de adoção administrativa própria; fora deste patch.

## Permissões

Restore: descoberta regional, StartSession exclusivamente na EC2 de destino e
AWS-StartSSHSession, TerminateSession com as duas tags AWS (target e aws:userid).
Segue o plugin já validado: canal via token StartSession, sem inventar permissão
OpenDataChannel wildcard. Referência: [AWS sessões federadas](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-examples.html).

Dados da origem: ListBucket/ListBucketVersions/GetObjectVersion e localização.
Backups: listagem somente backups/ e leitura de versões nesse prefixo.
Destino: verificação das proteções, inventário, leitura exata por versão e cópia
PutObject com AES256/ACL private, PutObjectAcl private. Sem DeleteObject/Version,
PutBucketPolicy, SendCommand, shell SSM, SG write, IAM admin ou Terraform state.
Policy inline e boundary são idênticas, com Denies para APIs fora do escopo,
state, escrita na origem/backups e outros destinos SSM. A role EC2 do destino
mantém as escritas normais do Nextcloud SOMENTE no bucket novo, não em backups.

Trust segue os claims/subject imutáveis já utilizados no projeto e exige main,
Environment original e workflow `Restore platform (manual)`. Confirmar novamente
as configurações efetivas do GitHub na ativação; nome do workflow não substitui
proteção de branch/Environment. Nenhum Secret foi configurado neste patch.

## Ordem administrativa futura (autorizações separadas)

1. Configuração privada bootstrap: github_restore com destino/bucket independentes,
   sem target_instance_id. Plan salvo: exigir somente uma nova host boundary.
2. Apply bootstrap SOMENTE após revisar o plan real e autorização.
3. Configuração privada do root restore: boundary criada, nomes/CIDRs/chave pública
   aprovados e backend/key separados. Plan salvo: exigir somente recursos novos
   do destino. Nenhuma alteração ou exclusão na origem. Não usar deploy atual para
   criar esse destino antes dos guards: o lease inicial continua fail-closed.
4. Apply administrativo do destino após autorização, sem instalar aplicação.
5. Inserir o ID novo em github_restore. Plan bootstrap: somente três criações da
   identidade restore. Apply após revisão/autorização.
6. Verificar SSM e host key por canal administrativo independente. Preparar guard,
   supervisor/lease e identidade/config privadas correspondentes ao destino.
   Esses arquivos devem apontar ao bucket NOVO; não copiar bindings da origem.
7. Instalar código revisado, imagens e canal pelos playbooks separados de
   [PR8](phase-7-pr8.md); cadastrar Variables e RESTORE_SSH_PRIVATE_KEY no Environment.
8. Janela autorizada de restore com um backup específico ou latest_valid. Verificar
   login/download/identidade antes de declarar restore_proven. Nenhum Destroy aqui.

Comandos indicativos, somente depois das autorizações/configurações privadas:

```text
terraform -chdir=terraform/aws/bootstrap plan -lock-timeout=5m -var-file=PRIVATE_BOOTSTRAP_TFVARS -out=PRIVATE_BOOTSTRAP_PLAN
terraform -chdir=terraform/aws/bootstrap apply PRIVATE_BOOTSTRAP_PLAN
terraform -chdir=terraform/aws/restore init -backend-config=PRIVATE_DESTINATION_BACKEND
terraform -chdir=terraform/aws/restore plan -lock-timeout=5m -var-file=PRIVATE_DESTINATION_TFVARS -out=PRIVATE_DESTINATION_PLAN
terraform -chdir=terraform/aws/restore apply PRIVATE_DESTINATION_PLAN
```

Não usar -upgrade, -lock=false, force-unlock, state rm ou key do state original.
Plan real pode indicar drift; não aceitar outras mudanças para acelerar a execução.

## Custo e recuperação

Uma segunda EC2, EBS e IPv4 público geram cobrança durante o ensaio; objetos
copiados, versões e requests S3 também. Não há endpoints privados/NAT adicionais.
Backups e origem continuam preservados. Interromper/terminar o destino não é
rollback de uma restauração parcial nem autorização de limpeza. Em falha, manter
journals e recursos para diagnóstico; não apagar tabelas/objetos ou repetir sozinho.

## Validação

Executar fmt/validate em cópias sem backend, com mirror AWS 6.67.0 existente.
Tests Terraform usam mocks; sem credenciais reais, IMDS ou chamadas operacionais.
Tests semânticos usam o JSON da policy REAL produzido pelos mocks, não policy
reescrita no teste. Avaliador sintético não substitui AWS IAM Policy Simulator,
resource policies, trust efetiva, SSM real ou restore funcional.

Durante a preparação, o teste inicial de um módulo runtime com provider interno
não recebeu o mock e consultou identidade AWS; nenhum recurso/state foi alterado.
O destino foi isolado em root próprio, com provider mock direto e credenciais
sintéticas obrigatórias no ambiente de validação. Não repetir esse teste inicial.

Fmt do arquivo preexistente bootstrap/tests/recovery.tftest.hcl apresenta pendência
anterior; não normalizar esse arquivo fora do escopo. Novos arquivos passam fmt.

Sem commit/push, workflow, apply, instalação remota ou alteração AWS neste patch.
Os planos mock não aprovam apply nem Destroy; a leitura dos planos reais autorizados
está registrada abaixo.

Resultados locais: 6 testes Terraform bootstrap + 4 testes Terraform destino +
27 testes semânticos IAM/snapshot: 37 aprovados, sem skips. Fmt dos arquivos
alterados, validate dos dois roots, sintaxe, links, scan de secrets e diff-check
aprovados. Plan mock do destino prevê 24 criações (23 AWS e terraform_data);
não é um plan de infraestrutura existente nem autorização de apply.
A boundary restore renderizada tem 4479 bytes, abaixo de 6144.

## Plans reais revisados — 2026-10-13

Preparação de leitura autorizada, sem apply. Bootstrap: somente 1 criação,
restore_host_boundary; destino: 24 criações, 0 alterações, 0 exclusões e nenhuma
substituição. Inclui 23 recursos AWS novos e terraform_data. Conta/região
conferidas; EC2 T3 medium, gp3 30 GiB criptografado e IMDSv2 required/hop2.

O drift da role de backup restringe-se ao campo computado inline_policy; seu
conteúdo corresponde semanticamente à policy inline já gerenciada pelo root.
Não há mudança de permissões dessa role no plan. Uma autorização futura de apply
deve reconhecer expressamente esse refresh computado; qualquer outro drift
continua bloqueador.

Backend S3 do destino foi preparado em cópia privada com key independente e
locking normal. Depois do plan, confirmou-se ausência de state do destino e
liberação do lock. Nenhuma infraestrutura foi criada. Os planos/JSON/configurações
ficam em diretório privado com ACL restrita e ignorado pelo Git, não nesta documentação.
IPv4 administrativo atual coincide com os CIDRs configurados neste plan; reconfirmar
se houver demora antes de aplicar.

AWS Price List API: EC2 Linux T3 medium em us-east-1, USD 0.0416/h; EBS gp3
USD 0.08/GiB-mês (30 GiB = USD 2.40/mês), além de IPv4 público e S3/requests.
Não inclui tributos, câmbio, tráfego ou custos da origem. Preços consultados para
estimar o ensaio, sem reservar/adquirir recursos.

A role OIDC de restore ainda não existe: só será planejada depois do ID da nova
EC2, para evitar acesso genérico. Próximo apply (autorização separada): boundary
e destino pelos dois planos salvos; depois plano específico da role, instalação
dos guards e janela de restore. Não executar deploy desprotegido para bootstrap
do destino, não limpar banco/volumes e não destruir origem.
