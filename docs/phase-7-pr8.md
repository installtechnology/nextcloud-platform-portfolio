# Fase 7 — PR8: restauração independente

## Atualização operacional — destino recriado em 14/10/2026

O mesmo backup foi recuperado na EC2 recriada: sete etapas concluídas, lease
released, Nextcloud/PostgreSQL/Redis healthy, login web e download de 196 bytes
com SHA-256 igual ao objeto selecionado do backup. O workflow terminou em failure
apenas no encerramento SSM; a conferência AWS posterior encontrou zero sessões
ativas. Houve intervenções administrativas e preservação de onze objetos extras.
Não é demonstração de automação sem intervenção nem de perda do bucket de origem.
Os flags restore_proven/destroy_authorized continuam false. Detalhes no
[resultado da recriação](phase-7-reprovisioning.md).

O resultado de 13/10 e as limitações descritas abaixo são o registro histórico
do primeiro restore independente, anterior ao ciclo acima.

Estado atual: **restauração automatizada executada com sucesso em destino AWS
independente em 2026-10-13**, com validação manual de acesso e download relatada
pelo operador. Banco, configurações e 173 objetos foram recuperados; os três
serviços principais estão saudáveis. O resumo atual está abaixo. As seções de
preparação preservam o histórico de suas entregas, não o estado atual.
O primeiro backup real do PR7 teve completion e integridade verificados; isso,
isoladamente, não comprova recuperação nem autoriza Destroy.

## Resultado real e limites da evidência

- Workflow `restore.yml`, commit `8a0cfcbeed104a6f792429d43e5dfa1cc1598c59`:
  job concluído com `success`, duração de 2min26s. IDs de operação, backup,
  instâncias, buckets e o vínculo exato permanecem no registro privado.
- Sete etapas concluídas: leitura e integridade dos artefatos; arquivos privados;
  PostgreSQL; cópia das versões S3 exatas; remapeamento explícito ao novo destino;
  início sem instalação; verificação da aplicação após reinicialização.
- 173 objetos copiados ao bucket independente, com VersionIds e bytes verificados.
- Nextcloud 34.0.4: HTTP 200, `installed=true`, `maintenance=false`, sem upgrade
  de banco pendente. Nextcloud, PostgreSQL e Redis `healthy`.
- Fencing PostgreSQL restaurado; lease `released`; origem preservada e desligada.
  Restore, deploy e backup operacional novamente desabilitados.
- Uma tentativa antiga permaneceu na fila do GitHub. Sua aprovação foi revogada
  sob o lock compartilhado e preservada. A aprovação ativa foi vinculada somente
  ao novo run, sem liberar execuções antigas ou criar retry automático.
- **Validação manual relatada pelo operador:** acesso autenticado ao destino,
  arquivo presente e download concluído. Foi fornecida uma captura da listagem;
  ela não foi versionada e não substitui inspeção de conteúdo ou checksum.

O download não foi comparado por SHA-256 com o original, e não foi executado um
segundo dispatch de restore para comprovar idempotência operacional. Não foi
emitido `restore-evidence-v2` com todos os checks obrigatórios: não preencher
checks pendentes com resultados sintéticos. Manifesto e journal continuam com
`restore_proven=false` e `destroy_authorized=false`. O resultado real comprova a
sequência realizada, sem conceder autorização para Destroy nem homologar todos
os cenários de falha.

## Reprovisionamento: o que já existe e o que falta

O workflow de deploy possui `purpose=new_installation|prepare_restore`, separado
de `mode=plan|apply`. `prepare_restore` instala ferramentas por Ansible e prepara
o destino, sem iniciar Nextcloud ou executar `maintenance:install`; `restore.yml`
é a operação posterior, com seleção explícita do backup.

Essa preparação exige um **destino já identificado**, com SSM/SSH autenticado e
lease/guard ativo antes de qualquer apply. `restore_preparation()` valida o ID
contra `RECOVERY_INSTANCE_ID`; `guarded_deploy()` abre o lease antes do Terraform
e confere novamente o ID/bucket contra os outputs runtime. Não há fallback para
um host novo sem guard. A role Ansible instala código revisado, dependências
verificadas e binding privado; o canal dedicado de restore é preparado pelo
playbook administrativo separado.

O destino deste ensaio foi provisionado e ativado administrativamente pelo root
`terraform/aws/restore`, com state e bucket separados. Isso **não valida** que
`deploy.yml` consiga, sozinho, recriar o runtime depois de destruir o host que
fornecia seu lease. Também não se deve apontar o workflow ao root/state da origem
para administrar o destino independente.

Antes de autorizar esse ciclo, resolver o bootstrap dos controles do host novo e
o rebinding do ID/host key/permissões exatas ao novo destino, preservando a
exclusão mútua administrativa e os gates atuais. Gerar e revisar plans específicos
sem exclusões de dados. Nenhum bypass de lease ou alteração remota foi feito
nesta revisão documental. Destroy seguro e o ciclo completo permanecem pendentes.

O [desenho mínimo de reprovisionamento](phase-7-reprovisioning.md) identifica as
dependências que exigem decisão arquitetural antes de implementar o caminho novo.
Não é um launcher nem uma autorização operacional.

## Seleção explícita

- `specific`: exige backup_id. Se inválido, incompleto ou indisponível, aborta;
  nunca substitui silenciosamente pelo último.
- `latest_valid`: opção explicitamente escolhida. Ordena conjuntos pelo
  completed_at validado e pelo backup_id como desempate determinístico. Lista
  candidatos rejeitados com códigos sanitizados; não oculta a escolha de um mais
  antigo. Erros de rede, leitura ou disponibilidade ambígua abortam a seleção.

O selector fixa VersionId do completion, hash/versão do manifesto e versões dos
cinco artefatos. Valida schemas v2/v1 existentes, relações semânticas, identidade
da instalação, bucket, SSE-S3, tamanhos, SHA-256 em streaming e dependências S3.
Consulta novamente a seleção corrente do completion antes de retornar. Depois
de selecionado, o futuro executor deve usar esse conjunto fixo, sem procurar
`latest` novamente durante a execução.

`BackupReader` só expõe ListObjectVersions, HeadObject e GetObject, com proprietário
esperado, HTTPS regional, verificação TLS e SDK explicitamente injetado. Não cria
clientes nem descobre credenciais. Nenhuma chamada AWS é executada pelos testes.
Não utilizar a role de deploy ou de backup para escrever o destino.

## Destino e procedimento

Um restore independente exige nova EC2, banco vazio, volumes próprios e bucket S3
de destino separado de dados/backups/state. A preparação rejeita os identificadores
da origem. Os adaptadores reais de identidade/inspeção foram escritos, mas não executados
contra um destino real; os testes locais não provam a condição de um host AWS.

1. Preparar infraestrutura isolada e Ansible, incluindo controles PR7 de forma
   idempotente. Não usar o instalador inicial em dados restaurados.
2. Baixar versões exatas dos artefatos diretamente no destino protegido. Não
   transportar configs/secrets pelos artifacts ou logs GitHub.
3. Restaurar roles/grants/extensões, credenciais protegidas e dump PostgreSQL 17.
   Verificar ownership. Não executar SQL sobre banco existente.
4. Restaurar config/identidade e arquivos locais com validação de archives,
   ownership e links. Não executar `tar extractall` sem confinamento.
5. Copiar somente objetos visíveis nas versões inventariadas para o bucket vazio
   de destino. Não reativar chaves ausentes cobertas por delete markers, nem copiar
   versões mais novas. Registrar correspondência entre versões origem/destino.
6. Aplicar remapeamento explícito do bucket na config e marcador, com autorização
   própria e preservação da identidade. Manter storage guard ativo.
7. Iniciar serviços; verificar installed=true, PostgreSQL/Redis, login, download
   e hashes de arquivos sintéticos conhecidos, upload separado e reinicialização
   idempotente. Não usar o acesso funcional à origem como prova do restore.
8. Produzir evidência separada compatível com restore-evidence-v2, vinculada ao
   manifesto exato. Manter manifesto original restore_proven=false.

## Limites e próximas implementações

| Componente preparado | Validação e limite |
| --- | --- |
| Seleção e leitura regional S3 | Algoritmo testado com respostas sintéticas; nenhuma chamada AWS |
| Download para staging privado | Versão, SSE-S3, tamanho e SHA-256 em streaming; dados fictícios |
| TAR | Rejeição de traversal, duplicatas, links externos/hardlinks e arquivos especiais; extração em árvore nova, hash antes/depois; ownership real ainda não aplicado |
| Parâmetros protegidos | Campos/identidade/versões/secrets obrigatórios e limites Base64; não executa PHP, Compose ou globals SQL |
| PostgreSQL 17 | Algoritmo com inspeção de banco vazio, roles/extensões, catalog e pg_restore transacional; adapter Docker supervisionado e comparação restritiva de roles preparados; execução validada somente com mocks |
| Preparação do host | Playbook separado, identidade IMDSv2 verificada antes das roles, configuração privada e dependências verificadas; não integrado ao deploy, não executado no Linux |
| Objetos S3 | Adapter de cópia por VersionId, ACL private, SSE-S3 e comparação dos bytes origem/destino; somente simulador, sem exclusão e sem retry automático |
| Sequência/journal | Intents duráveis, vínculo ao conjunto/destino, bloqueio de estado interrompido e segunda execução sem reinstalação; adaptador de destino integrado localmente; não executado na AWS |

CopyObject limita objetos individuais a 5 GB; esta implementação aplica um limite
conservador de 5.000.000.000 bytes e aborta antes da escrita se precisar de multipart
copy. Não existe fallback por outra versão ou redução da integridade.
[Referência oficial CopyObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CopyObject.html).

O executor integra os adaptadores reais de host, arquivos privados, PostgreSQL,
S3 e remapeamento. Ainda faltam a instalação/ativação no destino autorizado,
IAM/canal dedicado e workflow manual de restore, além da homologação real.
O adapter PostgreSQL confere roles e metadados, mantém fencing durante pg_restore
e usa a role não privilegiada da aplicação. Não executa globals SQL arbitrários.
Os métodos injetados da sequência não são implementações operacionais dessas etapas.
Não há launcher ou mecanismo de ativação operacional nesta entrega, nem emissão
de restore_proven=true. Em falha, o destino parcial e os journals são preservados;
nunca apagar tabelas/volumes ou alterar a origem para tentar novamente.

Preservar EC2 original, PostgreSQL, volumes e objetos/versões de origem durante
todo o ensaio. Antes de criar o destino, revisar custos, plan e IAM e obter
autorização. Destroy continua fora desta entrega. O backup atual depende do
bucket original e das versões; não protege contra perda completa desse bucket.

## Validação local

Com as dependências já verificadas do PR2 no PYTHONPATH:

```text
python -m unittest discover -s scripts/restore/tests -v
```

Fixtures são exclusivamente sintéticas. Testes extraem TARs pequenos e escrevem
journals em diretórios temporários; PostgreSQL, S3 e sequência de host usam mocks.
Não executam AWS, SQL, Docker, download real ou manutenção. Linux, criação efetiva
dos symlinks, ownership e containers reais ainda exigem homologação específica.
Nenhum resultado concede autorização de Destroy ou prova de recuperação funcional.
Não foram feitos commit ou push.

Resultado da suíte PR8 após revisão final: **191 testes locais aprovados, zero skips**.
Sintaxe Python, links locais, scan redigido de secrets e diff-check aprovados,
incluindo arquivos novos. Foram alterados/criados 38 caminhos, limitados a scripts/restore, documentação,
preparação Ansible separada, controlador deploy e dois workflows existentes.
Terraform, IAM, Compose operacional, contratos e workflows de backup permanecem
intactos. O deploy recebe a escolha explícita descrita abaixo; sua concorrência,
lease obrigatório e confirmação APPLY permanecem preservados. Ansible não está disponível neste Windows: validação YAML e
testes estáticos executados; syntax-check Ansible e ensaio Linux permanecem pendentes.

Regressões reexecutadas nesta preparação: 73 testes do contrato, 187 aprovados do
validador e 2 skips POSIX, 15 testes do controlador de deploy, 16 aprovados do
storage guard e 1 skip dependente de plataforma, 18 testes de instalação inicial.
Esses skips não são falhas nem homologação POSIX para o código novo. Nenhum teste
executou o entrypoint em Docker ou restaurou um PostgreSQL real.

## Escolha no deploy — integração local

O input `purpose` é independente de `mode=plan/apply`:

- `new_installation` (default): conserva o playbook deploy.yml e a validação das
  credenciais de instalação.
- `prepare_restore`: valida configuração explícita de destino, bucket separado,
  identidade, pins e lease; executa apenas restore-prepare.yml. Não instala nem
  inicia Nextcloud/PostgreSQL/Redis. Exige SSH válido, sem exigir novas senhas da
  aplicação: os segredos recuperados serão tratados pelo executor de restore.

`RESTORE_HOST_CONFIGURATION` deve conter os campos exatos de host_scope.py,
sem credenciais. Conta/região/projeto/buckets precisam coincidir com o contexto
validado e instance com RECOVERY_INSTANCE_ID. O código é exportado apenas de
blobs Git da revisão exata, nas quatro árvores permitidas, sem arquivos ignorados,
symlinks ou alterações locais. Todas as wheels são verificadas antes dos applies.
O preflight Ansible consulta IMDSv2 e rejeita dados locais, containers ou volumes
existentes antes de executar common/docker. Não substitui inspeção de vazio do
bucket nem o lease durante a execução de restore.

A escolha não habilita criação de destino por bypass: o guard Terraform atual
continua rejeitando substituição da instância vinculada ao lease. O bootstrap de
um destino novo e sua autoridade precisam ser integrados antes de uso operacional.
Não executar o deploy atual para sobrescrever a origem ou trocar seu state.
A preparação **ainda não é a execução de restore**; o executor está integrado localmente, mas faltam instalação/ativação no destino
e canal/workflow de restauração com identidade própria.

## Qualificação Linux preparada, não executada

O workflow manual existente backup-pr7-linux-validation.yml mantém contents:read,
sem Environment, OIDC ou secrets. Foram acrescentadas a suíte sintética PR8
(exatamente 191 testes, sem skips/expectedFailures/unexpectedSuccesses) e o
syntax-check restore-prepare.yml. O Python é o mesmo venv privado CPython 3.12
com a closure qualification verificada. Não há chamada Docker, AWS ou restore.
O runner tem acesso à internet para baixar wheels aprovadas; não é air-gapped.
A execução depende de revisão/publicação e autorização explícita separada.

Nesta rodada: 25 testes novos da preparação/dispatch e regressão dos 15 testes
originais do controlador aprovados; Actionlint dos dois workflows aprovado.
Os 179 testes são locais e sintéticos, não homologação de restauração funcional.

## Executor integrado — revisão local

`execute_restore` exige SelectedBackup e HostRuntime reais, cria Destination e
mantém RestoreControl.hold durante toda a sequência. RestoreControl reutiliza
Store/deploy_lease do PR7 no destino, com a mesma exclusão e journal que bloqueiam
backup e deploy. A configuração root-owned dos guards deve corresponder ao alvo;
nenhum mecanismo de first-install ignora esse requisito. Em falha, o lease fica
intervention_required; não há TTL, force-unlock ou recuperação automática.

Fluxo preparado:

1. Conferir alvo/controle, Docker vazio, imagens disponíveis e pins, espaço livre
   e bucket destino vazio/protegido (versionamento, SSE-S3, ACL compatível, bloqueio
   público e TLS). A estimativa de disco é conservadora, não garantia contra ENOSPC.
2. Baixar os cinco artefatos por VersionId exato, revalidar hashes locais, extrair
   em staging novo e comparar todo o config entre os dois TARs. Código recuperado
   precisa coincidir exatamente com o código revisado; não executar scripts de um
   backup antigo incompatível sem revisão explícita.
3. Preparar somente PostgreSQL/Redis/Nextcloud. Não iniciar observabilidade com
   secrets não incluídos no backup. Segredos mantêm o padrão de mounts do deploy:
   arquivos legíveis pelos containers dentro de diretório host root 0700.
4. Inicializar PostgreSQL vazio com os segredos recuperados, verificar globals,
   roles/metadados, aplicar fencing e executar pg_restore transacional supervisionado.
5. Copiar objetos atuais por suas versões capturadas e comparar bytes/hashes,
   registrar o mapa de versões destino e manter delete markers ausentes.
6. Remapear **somente staging** em helper sem rede, sem log Docker, com rootfs
   readonly e imagem fixada. Preservar instanceid/passwordsalt/secret, installed,
   banco e prefixo; rejeitar qualquer config/marker incompatível ou credencial AWS
   estática. Manter a configuração esperada pelo storage guard original.
7. Publicar a árvore HTML em volume novo/vazio, com ownership conferido, sem
   sobrescrita. Iniciar com restore-start.sh: storage guard + assert-restored.php,
   sem maintenance:install ou native auto-install. Verificar occ status e health,
   reiniciar uma vez e verificar novamente. Manutenção original permanece igual;
   se true, seu healthcheck habitual não pode ser interpretado como instalado=false.

Comandos de host têm allowlist literal e intents duráveis antes de Compose/helper.
Timeout deixa resultado ambíguo bloqueado; não inferir término de containers a
partir do término do cliente Docker. Helpers parados e journals são preservados;
processos/containers inesperados bloqueiam. Não são apagados volumes, tabelas,
objetos ou a origem. Reboot, SIGKILL e orfandade real continuam sem ensaio neste PR8.

O executor retorna apenas candidato. Não emite restore_proven=true, evidência
funcional ou autorização de Destroy. Login/download e integridade funcional do
usuário ainda exigem ensaio autorizado em destino independente.

Nesta rodada: 45 testes adicionais de arquivos, PHP, recipe, comandos/journal e
proteções de bucket; 179 testes da suíte completa aprovados. PHP lint aprovado.
Mocks de Docker/SQL/S3 não comprovam execução dos adaptadores. A futura qualificação
Linux exige PHP CLI >=8.1 já disponível para as fixtures e falha se ausente; não
instala PHP ou aceita fallback. O ensaio na imagem Nextcloud fixada segue pendente.

Verificações finais desta integração: 179 testes PR8, 73 contratos e 15 testes do
controlador aprovados; 18 testes do instalador aprovados; 16 testes storage guard
aprovados e 1 skip de plataforma. Sintaxe Python/JSON/YAML, PHP lint, shell syntax,
Actionlint, links, scan de secrets e diff-check aprovados. O syntax-check Ansible
real continua pendente (Ansible indisponível no Windows). Não foram executados
Docker/Nextcloud/PostgreSQL reais, workflows, AWS, commit ou push.
O mapa local de versões possui limite de 900 KiB para continuar legível pelo Store;
um conjunto maior aborta sem promoção e preserva o destino para reconciliação.

## Arquivos locais do PR8

- `.github/workflows/backup-pr7-linux-validation.yml`
- `.github/workflows/deploy.yml`
- `ansible/playbooks/restore-prepare.yml`
- `ansible/roles/restore_prepare/tasks/main.yml`
- `docs/README.md`
- `docs/phase-7-pr8.md`
- `scripts/ci/deploy.py`
- `scripts/restore/__init__.py`
- `scripts/restore/archives.py`
- `scripts/restore/assert-restored.php`
- `scripts/restore/control.py`
- `scripts/restore/database.py`
- `scripts/restore/deployment.py`
- `scripts/restore/destination.py`
- `scripts/restore/downloads.py`
- `scripts/restore/executor.py`
- `scripts/restore/host_runtime.py`
- `scripts/restore/host_scope.py`
- `scripts/restore/objects.py`
- `scripts/restore/parameters.py`
- `scripts/restore/plan.py`
- `scripts/restore/postgres_adapter.py`
- `scripts/restore/prepare_identity.py`
- `scripts/restore/private_files.py`
- `scripts/restore/qualification.py`
- `scripts/restore/remap-config.php`
- `scripts/restore/restore-start.sh`
- `scripts/restore/roles.py`
- `scripts/restore/runtime.py`
- `scripts/restore/s3_reader.py`
- `scripts/restore/selection.py`
- `scripts/restore/sequence.py`
- `scripts/restore/tests/test_deployment.py`
- `scripts/restore/tests/test_executor.py`
- `scripts/restore/tests/test_host_preparation.py`
- `scripts/restore/tests/test_primitives.py`
- `scripts/restore/tests/test_selection.py`
- `scripts/restore/tests/test_sequence.py`

## Revisão final para publicação preparatória

Escopo preservado: 38 caminhos (4 modificados e 34 novos). Sem Terraform, IAM,
Compose operacional, contratos normativos ou workflow operacional de backup
alterados. Publicação não executa workflows: ambos os workflows modificados
permanecem exclusivamente workflow_dispatch. Instalação normal é o default;
prepare_restore exige configuração e lease válidos, sem fallback permissivo.

Correções nesta revisão:

- Compose pode criar HTML ao iniciar PostgreSQL/Redis. Aceitar esse volume apenas
  com nome/projeto/labels corretos, driver local sem options externas, mountpoint
  esperado, vazio e sem nenhum container usando-o. Não remover/recriar o volume.
- Revalidar SHA-256 do TAR ao ler metadata de ownership, antes da publicação HTML.
- Inspeção do controle valida binding completo do journal, steps, identidade,
  manifesto e completion; comando incompleto ou exec órfão mantém bloqueio.
  Links e entradas inesperadas no staging não são silenciosamente ignorados.

Foram acrescentados 12 testes de regressão específicos, totalizando 191. O código
continua preparatório: ensaio Docker/Linux real, syntax-check Ansible, canal/IAM e
workflow de restore próprios, instalação dos guards no novo destino e restauração
funcional permanecem pendentes. Não publicar como PR8 operacional homologado.
Nenhum teste autoriza Destroy ou concede comprovação de restauração por flags.

## Canal e workflow manual — continuação local

O executor preparatório foi publicado e os 191 testes anteriores passaram no
runner Linux no run 38348491252. Esta continuação acrescenta runner, canal,
dispatcher e preparação do acesso; **ainda não foi publicada nem executada no Linux
ou na AWS**. Não interpretar a qualificação anterior como homologação deste patch.

`restore.yml` é exclusivamente workflow_dispatch, main, confirmation=RESTORE,
Environment protegido e concurrency igual ao deploy/backup, cancel-in-progress=false.
Inputs escolhem specific + backup_id ou latest_valid + ID vazio. Contexto inválido
aborta antes do OIDC; não existe fallback para outro backup ou role. Permissão
contents:read global e id-token:write somente no job que assume a role dedicada.
Nenhum artifact, Terraform, password da aplicação ou credencial administrativa.

Runner usa exclusivamente `<project>-<github-environment>-restore`, nunca a role
de deploy/backup. Reutiliza os SDKs/wheels e o plugin verificados, SSM com
AWS-StartSSHSession e SSH com host key independente, user ncrestore e dispatcher
literal nextcloud-restore restore. Alias ncbackup-<instance> é apenas o padrão
já aceito pelo verificador de host keys; não concede permissões de backup.
Chave SSH dedicada temporária no runner é apagada ao sair; credenciais AWS ficam
em memória e seguem pelo stdin do canal SSH, não por argv/arquivos. Core dumps
são desativados. Dumps/configs continuam somente no destino privado, nunca no
runner. O plugin termina a própria sessão pelo fluxo SSM já implementado.

O host valida config/acesso root-owned, target IMDSv2, source/target distintos,
role STS efetiva, tags/profile/SSM, public IP atual confirmado por EC2 API, repo,
Environment e commit exato instalado. Preserva a manutenção anterior e retorna
somente RESTORE_CANDIDATE_READY, restore_proven=false, destroy_authorized=false.
Status/health não comprovam login/download ou recuperação funcional.

`nextcloud-restore-ssh` só aceita um comando literal; o wrapper limpa o ambiente.
Dispatcher fixa systemd-run com grupo completo, UMask0077, LimitCORE0 e timeout
2700s. Não aceita unidade, argv, paths ou comandos fornecidos pelo usuário. EOF,
SIGTERM, timeout e resultado desconhecido não autorizam retry, cleanup ou unlock.
O mesmo conjunto completo pode ser revalidado sem reinstalar; captura/restauração
parcial correspondente sempre exige reconciliação. Journals permanecem no destino.

### Preparação explícita do acesso

`restore-channel-prepare.yml` é separado do deploy. Exige confirmação
PREPARE_RESTORE_CHANNEL, Ubuntu24.04, host já preparado/verificado, configuração
administrativa válida e chave pública Ed25519 revisada. Instala somente ncrestore,
public key forçada, wrappers root-owned, sudo de um comando e access.json600.
Home e .ssh são root-owned para impedir substituição de authorized_keys.
Não instala privada, ativa serviço persistente, reinicia containers ou inicia SSM.
O guard compartilhado PR7, com config correspondente ao destino, é pré-requisito;
este playbook não cria bypass nem resolve bootstrap de nova infraestrutura.

access.json: version1, repository, branch(main), github_environment,
resource_environment, role_arn e source. Source é a identidade administrativa
esperada do manifesto (account_id, project, environment, identidade SHA256,
data_bucket, region). Sem passwords ou credenciais. GitHub Environment e
resource_environment são distintos: um controla autorização GitHub; outro,
tags/profile da EC2. Não presumir branch no subject Environment OIDC.

### Configuração futura e permissões pendentes

Variables: AWS_ACCOUNT_ID, AWS_REGION, PROJECT_NAME, RESTORE_RESOURCE_ENVIRONMENT,
RESTORE_ROLE_ARN, RESTORE_INSTANCE_ID, RESTORE_SOURCE_INSTANCE_ID,
RESTORE_KNOWN_HOSTS, SSM_PLUGIN_PACKAGE_SHA256. Environment input define o contexto
GitHub. Secret único do fluxo: RESTORE_SSH_PRIVATE_KEY, chave dedicada, nunca a
privada da aplicação/deploy. Não configurar esses valores automaticamente.

A role ainda precisa ser preparada/revisada no bootstrap com boundary obrigatória
antes de ativação AWS. Trust deve seguir o formato real do repo/audience/subject,
Environment e controles ref/workflow existentes, sem inventar claims. Environment
restrito a main; proteger alterações de workflow e revisão de execução. Nome do
workflow não substitui esses controles. Não reutilizar backup IAM integralmente.

Permissões mínimas previstas, a confirmar no plan/IAM antes de apply:

| Recurso | Operações necessárias | Exclusões |
| --- | --- | --- |
| EC2/SSM destino exato | DescribeInstances, DescribeInstanceInformation, StartSession com AWS-StartSSHSession, OpenDataChannel/TerminateSession da própria sessão federada | Outras instâncias, SendCommand, shell genérico, alteração SG |
| Backups, prefixo backups/ | ListBucketVersions e GetObjectVersion dos conjuntos | PutObject, DeleteObject/Version, policy/lifecycle/retention |
| Dados origem | ListBucketVersions, GetObjectVersion/HEAD por versão exata | Qualquer escrita/exclusão/fencing |
| Dados destino independente | ListBucketVersions, GetObjectVersion/HEAD, PutObject/cópia com AES256, PutObjectAcl condicionado a private; leitura de versionamento/encryption/public-access/ownership/policy | DeleteObject/Version, PutBucketPolicy/DeleteBucketPolicy, administração S3 |
| IAM e Terraform state | Nenhuma | Administração IAM, PassRole, state ou provisionamento |

Separar ARNs bucket/objeto e usar ExpectedBucketOwner; restrições/Denies efetivos
exigem avaliação de boundary e resource policies na revisão AWS autorizada. SSE-S3
não exige chave age ou KMS. Não há permissões de upload multipart-copy nesta versão;
objeto acima do limite existente aborta. Não ampliar automaticamente permissões.

Antes da primeira execução ainda precisam de autorização: prover o destino com
state/resources isolados, instalar seus guards/SSM e código da revisão publicada,
aplicar a role dedicada após plan, verificar host key/chave/Environment e cadastrar
Variables/Secret. Deploy atual continua exigindo guard já instalado antes de apply;
bootstrap inicial do destino requer procedimento administrativo próprio revisado.
Não reutilizar o state/source para contornar esse bloqueio. Após ativação, a janela
real de restore exige autorização separada. Origem continua preservada; Destroy
permanece fora deste patch.

Resultado local desta continuação: 233 testes sintéticos aprovados, sem skips.
Testes novos cobrem source/role/repo/commit incorretos, entradas ambíguas, resposta
sensível/false proof, concorrência de workflow e segunda execução sem reinstalação.
SSM real, systemd real, Ubuntu syntax-check novo e restauração funcional permanecem
pendentes. Nenhuma operação AWS, commit/push ou workflow foi executado nesta rodada.


Preparação de imagens: restore_prepare agora invoca prepare_images após verificar
config e dependências. Rejeita qualquer container/volume existente, puxa somente
os três digests fixados e confere RepoDigests; um erro de inspeção diferente de
ImageNotFound aborta. Não inicia containers. Essa tarefa ainda não foi executada:
será parte da preparação autorizada do destino, não desta rodada local.

Arquivos desta continuação (18 caminhos): .gitattributes; workflows README,
restore.yml e backup-pr7-linux-validation.yml; docs/phase-7-pr8.md;
ansible/playbooks/restore-channel-prepare.yml; roles/restore_channel/tasks/main.yml;
roles/restore_prepare/tasks/main.yml; scripts/restore/access.py, channel.py,
host_dispatch.py, host_worker.py, nextcloud-restore, nextcloud-restore-ssh,
runner.py, prepare_images.py, qualification.py e tests/test_transport.py.


### Preparação Terraform do destino

O desenho e a sequência administrativa estão em
[PR8 infraestrutura](phase-7-pr8-infrastructure.md). O patch local mantém
opt-in desligado e não aplica nem provisiona recursos automaticamente.


### Preparação do lease no destino (patch local)

O role restore_prepare passa a instalar o binding compartilhado PR7, root-owned
0600, após conferir seu parser original e todos os campos do destino. Prepara
/etc/nextcloud-recovery, /var/lib/nextcloud-recovery e /run/nextcloud-recovery
0700, sem instalar dispatchers/usuário de backup ou ativar captura.
As referências backup/guard/admin/emergency exigidas pelo formato PR7 permanecem
nomes reservados no namespace do destino, sem criar roles ou alegar autoridade
de fencing; não são usadas para conceder privilégios de restore. Somente o lease
local e o supervisor são usados pelo canal de restore dedicado.

239 testes locais aprovados, sem skips; a qualificação Linux anterior aprovou 233
e não cobre este patch novo. Instalação remota continua pendente de autorização.

Quando o guard já existe e nenhuma configuração administrativa nova é fornecida,
o playbook utiliza o loader estrito original e confere o destino, sem sobrescrever
o arquivo. Ausência ou binding incompatível bloqueia a preparação; não existe
fallback para guard vazio, flag ou origem. Uma configuração nova só é gravada
quando explicitamente fornecida e validada.

## Retomada administrativa limitada ao checkpoint pré-banco

Implementação preparatória aprovada para publicação e instalação separadas;
execução real não autorizada nesta alteração. Os contratos normativos, o workflow manual, a
role OIDC, os guards PR7 e o comportamento de instalação nova permanecem iguais.
Não existe retomada automática, expiração de lease ou opção de force-unlock.

O único checkpoint aceito é uma operação com `download_and_verify` e
`restore_private_files` concluídos, interrompida antes do `pg_restore`. A
retomada reutiliza essa operação e os containers PostgreSQL/Redis existentes.
Não remove volumes, reaplica arquivos privados, recria containers ou declara
etapas não executadas como concluídas. Falhas após fencing do banco, durante o
restore SQL, na cópia S3 ou em etapas posteriores continuam bloqueadas.

Antes de qualquer ativação, o administrador precisa verificar o run anterior
terminal, ausência de worker/execs/sessões pendentes, banco sem objetos de
aplicação, destino S3 vazio e vínculo ao backup/destino exatos. O registro
privado `/etc/nextcloud-restore/resume-approval.json` deve ser root:root `0600`,
sob diretório `0700`, e criado somente por autorização administrativa separada.
Não é Variable, Secret, input do workflow ou flag de simulador.

O formato interno está em `scripts/restore/resume.py`. Exige versão/kind,
binding completo da operação (backup, SHA do manifesto, VersionId de completion
e identidade do destino), commit aprovado, `run_owner` específico (`run_id:attempt`),
owner anterior, boot ID e hashes dos bytes atuais de journal, exec journal,
comando do host, lease e árvores privadas configuration/html/runtime. Nenhum
valor privado deve ser publicado. Esse registro não é uma evidência de
restauração nem modifica os schemas normativos.

O mesmo lock PR7 permanece obrigatório. Todos os bloqueadores alheios à operação
aprovada, execs órfãos, mudanças de boot, alterações em evidências, identidade ou
ownership impedem o claim. O registro `resume-claim.json` é criado uma única vez
por escrita exclusiva e fsync, antes da transição do lease; journal e lease
anteriores são preservados em arquivos privados. Reexecução do workflow, outro
run/attempt, alteração do registro ou interrupção durante o claim exigem nova
reconciliação explícita. O registro não é removido automaticamente.

Sob o lease compartilhado, o controlador revalida em streaming tamanho e SHA-256
local de todos os artefatos ligados às versões S3 selecionadas, árvores privadas,
código aprovado, recipe Compose, parâmetros, secrets e arquivos instalados. Não
imprime seus valores. Revalida containers/imagens/identidade, privilégios/locale
PostgreSQL, ausência de tabelas/sessões externas e proteções/vazio do bucket de
destino. Só então inicia a primeira etapa SQL. A retomada não executa `compose up`
para substituir PostgreSQL/Redis; verifica os IDs existentes e sua saúde.

Qualquer falha mantém o lease em `intervention_required`. Em sucesso, o lease é
liberado duravelmente. A sequência continua produzindo somente candidato:
`restore_proven=false` e `destroy_authorized=false`, inclusive em testes.

### Validação e limites

Os testes locais usam journals, arquivos e respostas sintéticos. Cobrem binding,
replay, owner/commit/boot divergentes, alteração de evidências, consume único,
falha de gravação do lease, órfãos, banco não vazio, sessões externas, roles,
container substituído e preservação de etapas/evidências. Mocks não demonstram
quiescência, persistência em reboot ou recuperação funcional. A instalação desta
alteração, a criação da autorização privada e uma execução real de retomada
continuam exigindo revisão/autorização separada. Não executar Destroy antes da
restauração funcional comprovada.

A suíte contém três testes de permissões reais POSIX/root (arquivo privado,
arquivo world-readable e symlink da autorização). No Windows eles são pulados
explicitamente. Após autorização específica do operador, os 36 testes sintéticos
da retomada passaram no sandbox temporário privado Linux, incluindo esses três
casos reais de permissões, sem failures/errors/skips. O sandbox foi removido ao
final. Foi utilizado somente o Python já instalado; nenhuma instalação do patch,
consulta PostgreSQL/S3, alteração de container, journal/lease ou restore foi
executada. Isso valida o código e as permissões em fixtures; não é comprovação
de retomada operacional ou restauração funcional. Publicação e instalação foram aprovadas separadamente. A autorização
administrativa privada e a execução real permanecem pendentes.


### Coordenação antes do claim

O Environment privado deste laboratório não dispõe de required reviewers no
plano atual (a API recusou a configuração com HTTP 422). A retomada não depende
desse recurso e não altera o plano, visibilidade ou permissões do repositório.

No checkpoint pré-banco, quando a autorização privada ainda não existir, o
worker aguarda no máximo 300 segundos. Essa espera ocorre antes do claim e da
transição do lease: não instala, extrai, executa SQL, altera S3 ou libera bloqueios.
O administrador prepara/verifica as evidências antes do dispatch e, ao obter o
ID do run autorizado, publica atomicamente a autorização root:root 0600, com
commit/run/attempt/backup/destino exatos. Ela nunca é criada pelo runner.

A autorização divergente, malformada ou insegura é rejeitada imediatamente;
alteração dos journals durante a espera aborta. Sem autorização no prazo, o
worker encerra com `RESTORE_RESUME_APPROVAL_TIMEOUT`, deixando o estado anterior
intacto. Não existe aprovação por tempo decorrido ou retry automático. Uma
instalação nova ou candidato já concluído não recebe essa espera. Esse ajuste
foi publicado e instalado, com 45 testes sintéticos aprovados no Linux. A única
retomada autorizada seguinte recebeu a aprovação exata, mas foi bloqueada pela
inspeção dos execs históricos antes de assumir o lease ou restaurar o banco.
Os testes da espera usam relógio fictício e dados sintéticos.

### Registros Docker exec históricos removidos pelo daemon

O Docker pode retornar HTTP 404 para um exec já encerrado. O supervisor distingue
exclusivamente `docker.errors.NotFound` com HTTP 404 de falhas de comunicação,
permissão e respostas inesperadas. A ausência não autoriza, isoladamente, uma
retomada: exige journal válido no mesmo boot, registro `finished` com exit code
inteiro entre 0 e 255, container original ainda em execução, ID ausente do
registro atual e inspeção sem execs ativos. Nenhum journal é apagado ou alterado
por essa leitura; registros incompletos, boot diferente e inspeções ambíguas
permanecem bloqueadores.

Terminação não significa sucesso: um exit code não zero permanece registrado e
os adaptadores continuam exigindo sucesso das operações, com a exceção explícita
das sondas de autenticação que devem ser rejeitadas pelo fencing PostgreSQL.
A autorização de retomada continua exigindo exatamente três consultas anteriores
concluídas com código zero e vinculadas ao journal aprovado. Não há force-unlock,
retry automático, comprovação de restauração ou autorização de Destroy.

Os testes locais cobrem 404, erros de rede/permissão, container divergente,
execs ativos, estado incompleto, boot alterado e confirmação não booleana. A
instalação do patch e outra execução real permanecem ações separadas, autorizadas
explicitamente; mocks não homologam pg_restore, S3 ou a aplicação restaurada.
