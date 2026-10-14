# PR7 — fencing administrativo temporário do bucket de dados

> Estado atualizado: a integração local está implementada e descrita em
> [PR7 integrado](phase-7-pr7-integrated.md). As lacunas e etapas futuras abaixo
> registram a evolução preparatória; não implicam ativação ou homologação na AWS.

Implementação local preparatória, sem autorização de execução. Nenhuma API AWS,
SSM, IAM, operação Terraform, manutenção, captura real, commit ou push foi executado.
O coordenador não possui fábrica SDK, launcher ou integração com workflows.
Todos os testes novos usam políticas, identidades e clientes sintéticos.
`backup_complete` permanece falso; não há emissão de completion operacional.

## Desenho implementado

Um coordenador administrativo separado recebe identidade, bucket e operação
explícitos. Não usa a role de backup nem o Instance Profile do Nextcloud. Os
fragmentos de permissões são somente código para revisão, sem attachment IAM.
Não concedem escrita de objetos, exclusão de versões ou DeleteBucketPolicy.
Credenciais de um cliente futuro precisam permanecer em memória, sem descoberta
implícita de profile/IMDS, debug ou persistência. Este canal ainda não foi ativado.

`fence_policy.py` preserva estruturalmente a política original, incluindo ordem
dos statements, IDs, grants e TLS. Acrescenta três Deny com Sids vinculados ao
hash da operação. Documentos ambíguos, duplicados, fences preexistentes ou tamanho
superior a 20 KiB são rejeitados. Formatos não suportados falham; não são convertidos
ou descartados silenciosamente. A reversão conserva a estrutura JSON; não promete
que o serviço devolverá os mesmos espaços/bytes da representação original.

| Restrição aditiva | Ações e recursos | Exceções |
| --- | --- | --- |
| Objetos | PutObject, DeleteObject/Version, AbortMultipartUpload, ACL/tag de objeto/versão, RestoreObject e replicação; bucket/* | Nenhuma, inclusive para administrador |
| Configuração autônoma | Lifecycle, versionamento, replicação, Object Lock e DeleteBucket; ARN do bucket | Nenhuma |
| Política | PutBucketPolicy e DeleteBucketPolicy; ARN do bucket | Apenas roles administrativa e emergencial explicitamente identificadas |

PutObject é a autorização IAM relevante para iniciar multipart, carregar partes
e concluir o upload. O fence cobre a conclusão de uploads iniciados antes dele;
não usa ações IAM fictícias com nomes de APIs. Uploads pendentes, listagem truncada
ou resultado ambíguo impedem a captura. O coordenador não aborta uploads nem remove
partes. Eles precisam ser concluídos pelos escritores antes da janela ou tratados
em procedimento separado autorizado. [Multipart oficial](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html).

GetObject/Version, ListBucket/Versions e ListBucketMultipartUploads não recebem
novos Deny. Isso preserva as autorizações de leitura existentes, sem conceder
novos Allow nem garantir que todos os leitores estejam autorizados.

Lifecycle não respeita bucket-policy Deny para expiração automática. O preflight
rejeita lifecycle, replicação ou Object Lock existentes; não os desativa. A
autoridade administrativa deverá verificar também ações autônomas já enfileiradas
e histórico relevante: ausência de configuração atual, sozinha, não comprova
que nenhuma ação anterior continuará. Resultado não demonstrável bloqueia a janela.
[Limitação do Lifecycle](https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucket-policies.html).

## Exclusão mútua e limite da API

PutBucketPolicy não oferece comparação condicional por hash/If-Match. Um
read-compare-write isolado não impede outra alteração entre leitura e escrita.
Por isso `PolicyAuthority` falha por padrão: a implementação real precisa obter
controle administrativo exclusivo de TODOS os editores de política/configuração
antes da primeira leitura. Inclui Terraform, outros administradores, automações
e acesso emergencial. Não existe fallback para uma flag de aprovação.
[API PutBucketPolicy](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketPolicy.html).

Hashes e readback detectam alterações, mas não substituem essa exclusão externa.
O Deny de política reduz editores durante a janela; as duas roles excepcionadas
continuam sujeitas ao freeze. O lock PR3 permanece adquirido durante fence,
captura e reversão. `CaptureStoreView` permite a chamada reentrante do engine
somente na mesma thread e janela, evitando um segundo flock e deadlock. Após
liberação ou saída, a view é inválida. O journal de fence bloqueia o gate de
deploy, inclusive quando o controlador morreu e o lock do processo foi liberado.

Essa proteção é biblioteca local. O deploy publicado ainda não chama o gate
remoto antes dos applies nem mantém o lease durante Ansible/Compose. A integração
do serviço no host e do workflow é requisito anterior ao primeiro backup real.
O grupo de concorrência Actions não protege contra editores externos ou órfãos.

## Ownership, journal e quiescência

Antes da alteração: política original, SHA-256 dos bytes e da forma canônica,
política esperada/hash, operação, conta/roles, sessão administrativa original e
intent durável. O estado ativo é gravado antes de chamar PutBucketPolicy. Após
resposta: nova leitura e comparação, com evidência temporal no journal. Nenhum
token, AccessKey ou secret entra nesse registro.

O armazenamento reutiliza o Store PR3: confinamento, arquivos privados, escrita
atômica e fsync disponíveis no sistema. Não criar outro journal daemon. A
durabilidade no host Linux, disco cheio, reboot e SIGKILL do coordenador novo
ainda exigem ensaios descartáveis. Os testes em memória comprovam sequência e
decisões; não comprovam persistência física diante de perda de energia.

`WriterBarrier` e `HostProbe` não têm implementação permissiva. Antes do dump,
exigir bloqueio e drenagem de HTTP/cron/CLI/background, sessões e novas conexões
PostgreSQL, processos externos/root/Docker e escritores S3 identificados. O
backend verifica identidade, imagens, mounts, configuração e execs; não supõe
que essas observações implementam o fence de futuras conexões. Não mata sessões
externas automaticamente. Duas listagens iguais e readback não são prova de
ausência de escritas em trânsito ou de propagação instantânea da política.

Os checkpoints exigem evidência independente dos escritores, ausência de
multipart pendente, hash da política e inventários estáveis. Os hooks usam a
projeção PR5 com keys, VersionIds e delete markers preservados; vinculam EC2,
identidade instalada, captura, consistency_id, fence e instantes. Listagens
inconsistentes continuam rejeitadas, incluindo AMBIGUOUS_CURRENT_VERSION. Esses
recibos não promovem o candidato PR3 nem comprovam restauração.

O backend separado mantém os guards operacionais PR3 intactos. PostgreSQL e
Redis não são parados/recriados. O Nextcloud usa encerramento gracioso e estado
anterior de manutenção. Execs conhecidos e desconhecidos ainda ativos ou sem
inspeção conclusiva impedem retomada/liberação; nenhum comando é isento por nome.
Um healthcheck transitório pode bloquear conservadoramente: validar o collector
com o daemon real antes de autorizar uso, sem ignorar execs indiscriminadamente.

## Falhas, rollback e recuperação administrativa

Não há expiração nem liberação por timeout. Perda de rede, resposta desconhecida,
SIGKILL ou perda do controlador deixa a operação bloqueada até reconciliação.
Um erro antes de aplicar pode deixar apenas journal bloqueado: não afirmar que
um fence remoto existe sem readback. Uma resposta perdida após aplicar pode
deixar a política instalada. O código não repete escrita cegamente.

Liberação exige chamada explícita, mesma sessão administrativa e prova de que
execs/sessões encerraram e o estado próprio de manutenção/serviços foi recuperado.
Reversão: validar journal/ownership, comparar política atual com a exata política
fenced esperada, registrar restore_intent, comparar novamente, restaurar somente
a política original e conferir readback. Não remover Sids procurando apenas nomes.

Reconciliação administrativa explícita admite uma nova sessão da mesma role,
preservando e registrando ambas. Se a política já for a original após resposta
perdida, só reconhece isso nos intents compatíveis ou no journal de reversão
comprovada cujo ponteiro ativo não pôde ser atualizado. Qualquer alteração legítima
de terceiro impede restauração automática: preservar policy atual, journal e
evidências, obter revisão humana do diff e autorização específica para a correção.
Nunca sobrescrever com o snapshot antigo sem essa revisão.

Procedimento emergencial futuro: verificar primeiro identidade e acesso das duas
roles, política/SCP/endpoint e hashes; confirmar encerramento dos processos e
estado da aplicação; comparar o diff protegido. A role emergencial precisa de
Allow independente para ler/alterar policy: a exceção do Deny não concede acesso.
O owner root tem exceção S3 para Get/Put/DeleteBucketPolicy, mas SCP/endpoint pode
impedir esse caminho. Não disponibilizar root no runner. Alteração manual emergencial
exige autorização e registro; DeleteBucketPolicy indiscriminado removeria TLS e
outras proteções e não faz parte do procedimento automático.

A janela torna indisponíveis uploads, exclusões e outras escritas, inclusive
por terceiros. Leituras autorizadas permanecem. Estimar e medir duração somente
após preflight de espaço/volume de dados; falhas podem prolongá-la até intervenção.
Não iniciar a janela sem responsável administrativo, recuperação validada e
aceitação dessa indisponibilidade. Não há custo de infraestrutura criado por este
patch; futuros inventários/verificações geram requisições S3 e tempo EC2.

## Arquivos locais preparados

Todos em `scripts/backup/operational/`, ainda não publicados:

- Novos neste incremento: fence_policy.py, fencing.py, fence_admin_api.py,
  capture_backend.py, inventory_projection.py, inventory_hooks.py, read-state.php.
- Testes novos: tests/test_fencing.py, tests/test_fence_admin_api.py,
  tests/test_capture_backend.py, tests/test_inventory_hooks.py,
  tests/test_read_state_operational.py.
- Incrementos em módulos locais anteriores: interlock.py, supervision.py,
  docker_api.py, tests/test_docker_api.py e tests/test_supervision.py.
- Documentação: este arquivo; atualização de phase-7-pr7-ssm.md e
  phase-7-pr7-operational-path.md. O plano histórico de sandbox permanece intacto.

Terraform, IAM remoto, workflows, Ansible, Compose operacional, contratos PR1 e
guards PR3 não foram alterados. Imports não instanciam clientes/conexões/processos.

## Validação e caminho até o primeiro backup

Suite operational: 188 testes aprovados sem skips, usando PHP/Config oficial
Nextcloud já disponível localmente. Cobertura: políticas aditivas, reads,
multipart, ownership, journal adulterado, respostas perdidas, concorrência,
reversão explícita, alterações de terceiros, execs não rastreados, identidade do
alvo e binding dos inventários. Interrupções/timeouts são injetados; nenhum teste
destrutivo foi realizado na origem. O resultado completo das regressões está em
[SSM e supervisão](phase-7-pr7-ssm.md).

Bloqueadores reais, em ordem:

1. Implementar o coletor de host/writers e a autoridade administrativa exclusiva;
   identificar roles administrativa/emergencial e mecanismo de freeze efetivo.
   Não confundir esses interfaces fail-closed com um serviço pronto.
2. Concluir supervisor/lease remoto e integração do gate antes dos applies e
   durante Compose; ensaiar órfãos, Docker SDK, fsync e recuperação em destino
   descartável, nunca injetar SIGKILL/reboot na origem.
3. Completar fábrica SDK/SSM autenticado e produtor operacional de manifesto e
   completion; criptografia no host e verificação por VersionId/bytes já possuem
   blocos separados. Completion operacional continua bloqueado nesta versão.
4. Custódia externa da private age key definitiva; disponibilizar apenas recipient
   público na captura e validar capacidade de decriptação fora da origem.
5. Autorizações específicas: verificações operacionais read-only, IAM mínimo/SSM,
   instalação do supervisor/gates e eventual fence/apply administrativo. Somente
   depois autorizar uma janela e um backup real identificado. Nenhuma dessas
   operações está autorizada nesta entrega.

PR8 permanece posterior, em destino independente. Backup íntegro não é prova
de restauração nem autorização de Destroy; v1 depende do bucket de dados e versões.
