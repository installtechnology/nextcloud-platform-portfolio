# PR7 — caminho direto até backup real e PR8

> Estado atualizado: a integração local está implementada e descrita em
> [PR7 integrado](phase-7-pr7-integrated.md). As lacunas e etapas futuras abaixo
> registram a evolução preparatória; não implicam ativação ou homologação na AWS.

Decisão atual: utilizar EC2 existente, sem laboratório Windows/Hyper-V e sem
limitar o projeto pelo custo de mantê-la ligada. O plano local anterior permanece
histórico não publicado, não é uma dependência do PR7. Base f885202680b2ee01dcde1decab5df47812a7bdb3.
EC2 ligada segundo o operador; nenhuma consulta ou modificação remota autorizada
nesta etapa. SSM + SSH autenticado foi escolhido condicionalmente; não habilitado.

## Lacunas confirmadas no código, sem repetir auditorias aprovadas

| Componente existente | Reutilizar | Falta para operação |
| --- | --- | --- |
| Contratos PR1 e validador PR2 | Schemas, semântica, confinamento, hashing streaming | Coletar documentos/bytes de backup real; sem mudança normativa |
| Capture/Store PR3 | Lock OS, journal durable/intents, ownership manutenção, archives | Backend do alvo real; fencing/attestor e supervisor de subprocessos Docker |
| DockerBackend PR3 | Procedimentos de dump/config/read-state revisados | Não aceitar produção por override do guard nc-capture-test; adapter separado explícito |
| age PR5 | Encrypt-only v1.3.2 com hash/version check | Binário Linux aprovado no host e recipient definitivo com custódia externa |
| Inventário PR5 | Paginação, versões/delete markers, projeção e rejeições | API SDK real, evidência de escritores dentro da janela |
| Transport/SimulatedSeal PR5 | Algoritmos de hashes/VersionIds/colisão/completion | Produtor operacional separado, sem remover SIMULATION_ONLY |
| Preflight PR6 | Checklist como referência | Coletores reais de identidade/saúde/permissões/armazenamento; flags sintéticas não bastam |
| backup.yml PR6 | Dispatch/Environment/confirmação/concorrência | Integração explícita revisada, OIDC exclusivo backup e conectividade |
| IAM PR4 | Role/boundary dedicada e escopo data/backups | Opt-in administrativo, plan/apply separado e teste de permissões efetivas |
| Deploy | Concorrência Actions | Lock/lease/journal comum no host, antes de TODO apply e durante Ansible |

Nenhum comportamento será tornado operacional trocando simulation, synthetic ou
runtime_allowed por input/Variable. completion não significa restauração funcional.

## Desenvolvimento local iniciado

scripts/backup/operational/s3.py: adaptadores com cliente SDK injetado, nenhum
import boto3/discovery/network/CLI na construção. ExpectedBucketOwner, endpoint
HTTPS regional fechado, versão exata; URLs de keys decodificadas uma vez, VersionIds
opacos intactos. Escopos diferentes para dados e backups. Primitivas PUT condicional/
multipart só para nomes ciphertext autorizados. Não há APIs de exclusão, EC2/IAM,
completion ou fábrica de credenciais. simulation=False não habilita PR5 Transport.
TLS/timeouts/retries/credenciais da futura fábrica de cliente ainda devem ser
fixados e testados; metadata do cliente injetado não prova configuração TLS real.
Dependência SDK ainda não selecionada/instalada: preparar lock/provenance com
servicemodel compatível com IfNoneMatch em CompleteMultipartUpload, após aprovação
de dependências. Clientes falsos não provam compatibilidade SDK/API/permissões AWS.

scripts/backup/operational/interlock.py: contexto reutiliza Store.lock do PR3,
rejeita journal não terminal, inválido ou sem service_state_restored; exige inspeção
de processos e bloqueia remanescentes/estado desconhecido. Mantém lock durante
a mutação, não só um teste preflight. Não mata processos nem recupera manutenção.
Não integrado ao deploy; collector injetado não é attestation real.

Primeiro incremento: 31 testes locais, 23 S3 e 8 interlock; list_parts,
complete multipart condicional e abort do upload conhecido incluídos.
São primeiro incremento, não declaração de PR7 concluído ou homologado.

## Ordem curta de implementação e arquivos previstos

1. **Supervisão e exclusão mútua no host**, antes de qualquer captura real:
   operational/supervision.py, operational/capture_backend.py e testes;
   ansible/roles/recovery_capture e seu playbook dedicado. Reutilizar journal/lock;
   registrar operação, boot_id, container ID, Docker exec ID, PID/starttime e sessão
   PG associada. Morte do cliente não prova fim de docker exec. Supervisor systemd
   já previsto é adaptado, sem criar daemon remoto/broker genérico. Verificar processo
   e sessão antes de restaurar manutenção; resultado desconhecido bloqueia.
   Restart/reboot nunca liberam manutenção automaticamente. Recuperação explícita
   abandona candidatos parciais e só restaura estados próprios após prova segura.
2. **Proteção do deploy**, scripts/ci/deploy.py, .github/workflows/deploy.yml e
   ansible/playbooks/deploy.yml: gate remoto ANTES do persistent/runtime apply e
   lease no host mantido durante Ansible/Compose. Bootstrap da primeira instalação
   do guard exige etapa administrativa separada, pois não pode ocorrer depois do
   primeiro apply que ele pretende proteger. Missing guard não significa safe.
   Concorrência Actions continua existente, não substitui o host lock. Deploy e
   captura compartilham lock; deploy interrompido permanece bloqueado até resolução.
3. **Captura real e S3 durante a mesma janela**: backend novo verifica alvo/versões/
   imagens/config/identity reais; sem enfraquecer DockerBackend fixture PR3.
   Manutenção própria + bloqueio/drain do Nextcloud/cron/CLI, consumidores de volumes,
   sessões PG e escritores S3 externos. Sem controle demonstrado dos escritores,
   abortar ANTES do dump; inventários iguais não suprem esse controle.
   Capturar PG17 custom, globals sem role passwords, config/identity/local_files/
   parâmetros pelas rotinas existentes. Não parar/recriar PG/Redis, excluir volumes
   ou escrever no bucket de dados. Restaurar o estado original após confirmar fim
   de todas as tarefas; depois criptografar com arquivos locais protegidos.
4. **Completar adaptadores de transferência/produtor**, operational/s3.py,
   operational/seal.py e testes: integrar list_parts paginado, abort multipart conhecido,
   CompleteMultipartUpload condicional já preparados, recuperação de respostas perdidas sem
   retry cego, prefixo historicamente vazio, verificar bytes por VersionId exato.
   AbortMultipartUpload não é DeleteObject; já previsto em PR4. Não presumir que
   HEAD/metadata/ETag verifiquem ciphertext. Integridade plaintext/hash/ciphertext
   vem do age produtor; header age isolado não comprova criptografia autenticada.
   Manifesto criptografado e marker público por último, só após PR1/PR2 e todas
   dependências verificadas. Sem exclusão de objetos, versões ou expiração.
5. **Controle manual OIDC**, operational/preflight.py, operational/controller.py,
   .github/workflows/backup.yml e documentação PR7. Fábrica cliente com TLS/retries/
   limites fixos; role backup exclusiva; tokens/credenciais STS só em memória e
   canal autenticado. Plaintext nunca sai do host. Nenhuma private age key na EC2
   ou runner. Arquivos sensíveis locais0700/0600. Timeout antes da expiração STS.
6. **Publicação/revisão e autorização operacional**; nenhuma ativação escondida.
   Plano/apply IAM separado, instalação do host guard/supervisor separada,
   validação de leitura e uma execução real confirmada. Aprovação conceitual não
   é autorização de nenhuma dessas ações remotas.

Os caminhos acima são previstos. O estado implementado atualizado está em
[PR7 SSM e supervisão](phase-7-pr7-ssm.md). Nenhum workflow, Ansible, IAM ou guard
anterior foi alterado nesta preparação. Não existe captura operacional completa.

## Decisões realmente bloqueantes

**Conectividade escolhida:** SSM + SSH autenticado. Não reutilizar role deploy
para backup nem abrir SG via role backup. A opção escolhida depende de:

- SSM/SSH túnel ao próprio host, sem nova porta ingress: precisa agente/profile
  e permissões SSM restritas ao alvo/documento, mudanças explícitas em IAM/boundary
  PR4 e revisão de autenticação SSH/host key. Não supor SSM já disponível.
  SSH/port forwarding SSM não registra conteúdo da sessão: não alegar auditoria
  de comandos por esse mecanismo. Segredos nunca em parâmetros RunCommand.
Não implementar fallback para SSH público ou runner na EC2. Validar agente,
profile/boundary e saída HTTPS com autorização separada antes da integração.
[SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html),
[runner com IP fixo](https://docs.github.com/en/actions/concepts/runners/larger-runners).

**Credenciais S3 no host:** Instance Profile do Nextcloud permanece sem backup
permissions. Role backup OIDC pode emitir STS temporário e entregá-lo em canal
privado para processo do host (stdin/memória, sem shell env/log/artifacts/files);
validar expiração, usuário, processo receptor e limpeza. Isso exige revisão do
canal escolhido; não entregar GitHub token ou chave privada age ao host.

**Recipient e destino PR8:** operador deve controlar a chave privada definitiva
externa e validar que consegue recuperar/decriptografar. Gerar/receber essa chave
é autorização específica. Restore deve ter EC2/DB/volumes/bucket DESTINO distintos
e role própria sem escrita/exclusão na origem; custo e provisionamento separados.

## Validação mínima sem ensaios destrutivos na origem

Já aceitos: contratos/testes anteriores e cinco POSIX no Linux; não repetir seus
processos de aprovação. Para código novo: mocks de cada erro/resposta perdida/
interrupção e concorrência; testes Linux de lock/fsync/process supervision em
runner descartável sem AWS/credenciais. Docker exec órfão/SIGKILL/reboot devem ser
testados fora da origem; não realizar essas injeções na EC2 com dados.

Antes da janela real: autorização das validações read-only de identidade,
versões/containers, espaço/reserva, configs, writers/cron/sessões, disponibilidade
de versões S3, IAM/OIDC e host key. Testes IAM só usam operações do fluxo, não
upload/delete de objetos de dados. Permissão de escrever backups será validada
pela execução controlada, sem interpretar simulator/IAM JSON como prova AWS.

Primeiro backup: snapshot lógico único, janela medida, journal ativo antes da
manutenção, dump/config/inventários vinculados, restauração de serviço comprovada,
age no host, ciphertext transferido/verificado e completion por último. Uma falha
preserva artefatos/infrastrutura, bloqueia deploy se houver ambiguidade e não
provoca destroy/retry/maintenance off automático. Logs exclusivamente sanitizados.
Espaço EBS precisa comportar plaintext+ciphertext+reserva; medir antes. Se insuficiente,
apresentar dimensionamento e autorização, não aumentar volume automaticamente.

## PR8 — prova funcional independente

Depois de um backup completo/íntegro: selecionar ID explícito, autenticar marker,
manifesto e versões; decrypt em destino privado, restaurar roles/ownership e PG,
config/identity, copiar SOMENTE versões S3 selecionadas para bucket separado.
Aplicar remapeamento explícito de storage aprovado, sem desabilitar guard, sem
apontar nova instância ao bucket original como destino de escrita. Preservar keys;
novos VersionIds do destino são mapeados, nunca fingidos iguais aos da origem.
Ausências/delete markers não podem ressuscitar objetos excluídos.

Validar installed=true, identidade, login, download/bytes de arquivo conhecido,
apps e PG/Redis, reinicialização normal sem reinstalação; medir RPO/RTO e gerar
evidência separada vinculada ao manifesto exato, revisão humana. Sem Destroy.
scripts/backup/restore/, ansible/playbooks/recovery.yml e workflow manual de restore
são PR8 posterior; não implementados nesta entrega. Backup v1 ainda depende do
bucket original e versões; não protege perda total do bucket. Restore comprovado
não concede autorização implícita para destruir a origem.

## Estado deste incremento

Desenvolvimento/testes exclusivamente locais, clientes injetados. Nenhum acesso
AWS/EC2/S3, instalação, manutenção, captura, commit/push ou alteração operacional.
O fluxo real ainda não está implementado integralmente e continua bloqueado.
Fencing administrativo e backend separado preparados localmente em
[fencing PR7](phase-7-pr7-fencing.md); autoridade e collectors reais continuam
bloqueadores. Prioridade seguinte: integrar supervisor/collector ao backend real
e manter o lease remoto antes dos applies. Sem novas propostas de
VM no Windows. SSM escolhido não significa que sua compatibilidade foi comprovada.
