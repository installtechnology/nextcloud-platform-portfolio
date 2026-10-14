# PR7 — SSM, supervisão e bloqueios para ativação

> Estado atualizado: a integração local está implementada e descrita em
> [PR7 integrado](phase-7-pr7-integrated.md). As lacunas e etapas futuras abaixo
> registram a evolução preparatória; não implicam ativação ou homologação na AWS.

Desenvolvimento local, não homologado. Nenhum acesso AWS/CLI/EC2/S3/SSM, manutenção,
captura real, instalação de dependências, alteração IAM, commit ou push realizado.
Base local e referência origin/main: f885202680b2ee01dcde1decab5df47812a7bdb3;
origin/main não foi atualizado por fetch nesta etapa. EC2 ligada segundo o operador.

## Código implementado e limites

| Arquivo em scripts/backup/operational | Implementação | Limite explícito |
| --- | --- | --- |
| s3.py | SDK injetado, escopos separados, paginação, versões, ciphertext/multipart | Fábrica SDK e permissões efetivas não homologadas |
| interlock.py | Lock PR3 e journal/restauração/processos verificados | Collector completo não implementado nem integrado |
| supervision.py | Intents duráveis antes de create/start exec, inspeção daemon, boot ID, reconciliação explícita | SIGKILL/reboot reais não ensaiados; create sem ID exige intervenção |
| docker_api.py | Bridge Docker SDK, framing streaming, stderr descartado, timeout, stdin regular | SDK não instalado/fixado; interface SocketIO precisa ensaio Linux |
| postgres.py | PG17 Unix socket, custom/create, catálogo, globals sem passwords, sessões | Sem dump real, restauração ou garantia de fencing de futuras conexões |
| lease.py | Lock mantido e journal held/intervention_required/released | Sem TTL de liberação; host service/túnel e deploy ainda não integrados |
| ssm.py | Alvo explícito, role exclusiva, agente/Ubuntu online, doc SSH/22, token fora de argv, cleanup | SDK/plugin/SSH não executados; nenhum subprocess launcher fornecido |
| ssm_policy.py | Fragmentos IAM para revisão, conta/região/alvo/documento e sessão própria | Não anexados; não substituem revisão da policy/boundary completa |
| transport.py | Algoritmo PR5 separado para SDK real, byte/hash/VersionId e colisões | Nenhum retry de resultado ambíguo; completion rejeitado incondicionalmente |
| encryption.py | Candidato PR3 terminal/restaurado, quatro nomes/hash/tamanhos, Age encrypt-only | Não altera local_candidate_only; não cria inventário/manifesto/completion |

Os testes injetam clientes/arquivos sintéticos. Não são evidência de API AWS,
quiescência, TLS real, execução Docker, integração end-to-end ou recuperação.
Os construtores simulados PR3/PR5 e o workflow bloqueado PR6 permanecem intactos.
Não existe conversão por simulation/synthetic/runtime_allowed para operação.

## SSM escolhido: requisitos e permissões para futura revisão

SSH pelo SSM exige sshd e agente compatível. O projeto exige Agent >=3.3.40.0,
mais restritivo que o mínimo de SSH, para usar ssmmessages sem fallback
ec2messages. Confirmar versão, serviço snap/deb e perfil realmente associado no
Ubuntu 24.04; não presumir que a AMI assegura isso. Validar DNS e saída HTTPS/443
para ssm e ssmmessages regionais. Não é necessária nova regra ingress para o túnel.
[SSH SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-getting-started-enable-ssh-connections.html),
[agente Ubuntu](https://docs.aws.amazon.com/systems-manager/latest/userguide/agent-install-ubuntu.html),
[APIs de mensagens](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-setting-up-messageAPIs.html).

Instance Profile e **permissions boundary EC2** atuais só autorizam dados S3.
A preparação contém cinco ações de canal/registro, Resource * onde a API não
oferece ARN específico, restritas à região: UpdateInstanceInformation e
ssmmessages Create/Open Control/DataChannel. Não amplia S3 da aplicação.
Isso é candidato mínimo, não prova de funcionamento do agente: erros de
associações/inventário ou KMS, se presentes, exigem diagnóstico/revisão específica.

Role backup e boundary PR4 precisam incorporar conjuntamente StartSession no
ARN exato do alvo com Project/Environment e SessionDocumentAccessCheck, documento
AWS-StartSSHSession regional, OpenDataChannel para sessão própria, TerminateSession
com tags AWS de owner e target e DescribeInstanceInformation regional.
Discovery usa Resource *: seu filtro de InstanceIds restringe o pedido da aplicação,
**não é isolamento IAM por instância**. Não conceder SendCommand, RunShell,
ResumeSession, SG write ou IAM admin. Atualizar também o DenyUnlistedAPIs do PR4
em patch Terraform futuro revisado, sem desabilitar os outros Deny.
As expressões de sessão/principal federado precisam avaliação AWS separada.
[Autorização de sessões](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-examples.html).

Trust OIDC do PR4 continua referência: workflow e Environment corretos, branch
protegida no GitHub, role exclusiva. Deploy não será executado com role backup.
O gate remoto do deploy exigirá autorização própria, mínima, para seu canal;
isso precisa revisão junto à integração, antes de qualquer apply.

Plugin: o código oficial aceita AWS_SSM_START_SESSION_RESPONSE* como nome da
variável cujo valor contém a resposta. O adapter usa esse mecanismo, child env
privado, sem token em argv; limpa o dict após uso e sanitiza erros. Isso não
comprova disponibilidade na versão binária futura: fixar versão/hash/procedência,
verificar suporte e logging desativado antes de implementar o bridge concreto.
STS só em memória; sem profile, credential files, discovery IMDS ou debug. Não
prometer apagamento criptográfico de strings em memória Python.
[Fonte do plugin](https://github.com/aws/session-manager-plugin/blob/mainline/src/sessionmanagerplugin/session/session.go).

SSH exige chave já autorizada e known_hosts verificado independentemente,
StrictHostKeyChecking=yes, HostKeyAlias estável, sem accept-new/ssh-keyscan,
ForwardAgent=no, BatchMode e keepalive. ssm.py apenas prepara opções; launcher
deve conferir permissões/caminhos e fingerprint do arquivo. Túnel SSM SSH não
registra conteúdo de comandos no serviço: journal sanitizado será evidência local.

## Decisão arquitetural indispensável: escritores externos

Parar Apache graciosamente, interromper cron/background e observar zero sessões
PG não impede novas conexões PG nem Put/Delete S3 de outros principals/processos.
Inventários iguais não suprem isso. Não existe collector real capaz de atestar
esses controles no código atual; nenhum completion operacional pode ser emitido.

Desenho aprovado e **núcleo local implementado**, sem apply: administração
separada estabelece negações aditivas, ownership/journal, inventários vinculados
e reversão explícita. Role backup continua sem PutBucketPolicy. A autoridade
administrativa exclusiva e os coletores completos de escritores falham por padrão
até implementação real. Readback/hash não substituem exclusão: S3 não oferece CAS
para policy. Não existe launcher ou completion operacional.
[Fencing, testes e recuperação](phase-7-pr7-fencing.md).

Alternativa é uma fronteira de escritores fechada e demonstrável no host e na
autorização AWS. Declarar "só Nextcloud escreve" ou passar flag de confirmação
não comprova essa fronteira. Não implementar bucket policy/novas permissões sem
aprovação deste desenho. PostgreSQL precisa fence de novas conexões e sessões
externas, preservando o socket controlado de captura; não matar sessões de dados
automaticamente. Cron/CLI/root/Docker socket exigem freeze e inspeção explícitos.

## Caminho curto de ativação controlada

1. Concluir collector e autoridade administrativa do fencing aprovado; integrar
   backend separado do alvo e hooks já preparados, sem mudar contratos.
2. Completar serviço supervisor/lease remoto. Gate adquirido **antes** dos applies,
   mantido até fim do Ansible/Compose. Se canal/host cai, held/intervention_required
   bloqueia mesmo depois de flock ser liberado. Não auto-liberar por tempo.
   Não modificar deploy até possuir essa integração coerente/testada.
3. Integrar encrypt-only e inventários ao produtor separado; manifesto e marker
   por último, depois de bytes/VersionIds e contratos verificados. Não copiar
   dump/config plaintext ao runner. Simuladores não fornecem recibo operacional.
4. Fixar SDKs/plugin/Linux age e recipiente definitivo com custódia externa;
   testes destrutivos supervisor/Docker/systemd somente em ambiente descartável.
5. Autorização separada: leitura de agente/profile/egress/IAM, host key, imagens,
   identidade instalada, espaço e writers. Preparar plan administrativo mínimo,
   mostrar alterações e pedir autorização de apply IAM. Instalar host guard e
   supervisor somente com autorização própria; não ativar backup implicitamente.
6. Revisar/publicar integração manual e autorizar uma captura real identificada.
   Falha preserva dados/artefatos, bloqueia deploy quando ambígua e não dispara
   nova execução ou desligamento automático de manutenção.
7. PR8 posterior usa destino independente; integridade não prova restauração,
   v1 depende do bucket original/versões e não autoriza Destroy.

## Validação local desta preparação

188 testes operational aprovados, sem skips, incluindo PHP oficial disponível:
fencing, backend separado, hooks de inventário, S3/interlock anteriores e testes de
supervisor, PG, lease, SSM/policies, framing Docker, gates do transporte e ligação
de nomes/hashes da criptografia, falhas de journal/disco e interrupção injetada
do controlador. Isso não é execução real de sinais ou falta de disco. Testes de
criptografia deste adapter usam mocks;
roundtrip age real com identidade descartável pertence à regressão PR5.

Regressões: fixture 59, contratos PR1 73, PR2 187 aprovados/2 skips, PR3 71
aprovados/3 skips, PR5 144, IAM 21, inventário 29, PR6 146, guard 17,
instalação 18, controlador 13; todos aprovados. Os cinco POSIX são skips nesta
execução Windows; homologação Linux anterior aceita pelo operador permanece
registrada. Novos módulos Docker/SSM/lease não foram ensaiados em Linux real.
Não foi executado Terraform plan/test conectado ou novo teste Terraform neste
incremento: nenhum root foi alterado; IAM semântico usa output mock já existente.

O PR7 **ainda não está completo**: backend e núcleo do fencing estão preparados;
autoridade exclusiva, serviço/collector real, integração deploy, fábrica SDK,
produtor/marker e controlador manual operacional permanecem pendentes. Publicação
também não está autorizada. Falhas simuladas não homologam quiescência ou recuperação.
