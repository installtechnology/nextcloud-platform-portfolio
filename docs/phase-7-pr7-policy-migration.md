# PR7 — ownership administrativo da policy de dados

> Estado atualizado: a integração local está implementada e descrita em
> [PR7 integrado](phase-7-pr7-integrated.md). As lacunas e etapas futuras abaixo
> registram a evolução preparatória; não implicam ativação ou homologação na AWS.

Preparação local. Nenhum apply, destroy, import, state rm, migração de state,
IAM/SSM remoto, manutenção ou backup real foi executado. Sem commit ou push.
O PR7 continua sem homologação. O launcher local foi integrado, mas não instalado nem autorizado para execução operacional.

## Autoridade e diferença entre os fluxos

Antes, `persistent` gerenciava aws_s3_bucket_policy.tls e a role de deploy podia
executar PutBucketPolicy em dados e backups. Agora:

- O endereço tls é removido do ownership Terraform com `removed`/`destroy=false`.
  Isso exige um apply futuro de **forget somente**, não elimina o recurso S3.
  Todos os prevent_destroy dos buckets e os outros recursos persistentes permanecem.
- A role de deploy mantém PutBucketPolicy somente no bucket de backups. Um Deny
  explícito impede PutBucketPolicy/DeleteBucketPolicy no bucket de dados, mesmo
  se uma outra policy baseada na identidade tentar concedê-los.
- `data_policy_authority=null` não cria recursos. O opt-in cria role administrativa,
  boundary e inline policy idênticas, somente bucket de dados/ações necessárias.
  Sem PutObject, DeleteObject/Version, DeleteBucketPolicy, IAM admin, state ou
  backups. Não se altera a role de backup/Instance Profile da aplicação.
- O issuer é uma role administrativa da mesma conta, distinta de deploy, backup,
  EC2 e da própria autoridade. Nenhum OIDC workflow recebe essa identidade aqui.
  O broker administrativo futuro deve ser o único emissor comum dessas sessões,
  mantendo as credenciais em memória e os comandos serializados pelo coordenador.

O ownership é registrado em staging root privado pelo coordenador, não por um
novo root/state Terraform. Política anterior e hash, sessão, scope, proposta e
intent precedem PutBucketPolicy. Após readback, journal e ponteiro são atualizados.
Uma falha mantém adoção pendente; novas operações ficam bloqueadas. Reconciliação
remota é somente leitura: reconhece a policy esperada após confirmação perdida,
sem repetir escrita ou sobrescrever alterações de terceiros.

Na policy é acrescentado um Deny permanente de edição para outros principals,
com exceções somente das roles administrativa e emergencial. A política antiga
continua inteira e deve conter TLS obrigatório; sem ele, a adoção falha. Inicialização
nova produz TLS e guard administrativo somente após NoSuchBucketPolicy exato,
conferido novamente antes da escrita. AccessDenied/timeout não significam ausência.

Os paths antigos identificados são o recurso Terraform tls, a permissão
ManageTwoApplicationBucketsConfiguration, o controller `deploy.py terraform`
e qualquer CLI/API administrativa externa. Os três primeiros foram adaptados.
Ferramentas administrativas externas não são capturadas magicamente pelo lock.

## Fronteira de confiança e exclusão

RegisteredAuthority exige registro observado, scope, identidade administrativa e
guard permanente presentes antes da janela de fencing. O lock compartilhado
PR3 serializa os caminhos do broker/coordenador/deploy/captura cooperantes.
Compare/hash/readback detectam interferência; **não** substituem exclusão de
editores. A role de deploy perde o caminho direto pela mudança IAM futura.

O broker exclusivo ainda precisa ser instalado e validado; sua role emissora e
a role emergencial devem ser definidas pelo operador. Uma sessão administrativa
obtida fora desse broker é uma intervenção fora da fronteira de automação, não
algo que o flock impede. Root/administradores capazes de mudar IAM/SCP e o acesso
emergencial precisam de controle humano e registro separados. Não existe promessa
de proteção contra administração hostil ou garantia de CAS na API S3. Nenhuma
flag/Variable/Environment comprova exclusividade.

## Migração do ambiente existente — futura e separadamente autorizada

1. Congelar deploys, capturas e editores administrativos. Obter identidade,
   configuração, policy atual, state persistent e hashes por leitura autorizada;
   guardar cópias privadas fora do Git. Confirmar ausência de fence/journal pendente.
2. Revisar plan bootstrap da role/boundary/inline administrativa e redução de
   permissões de deploy. Definir broker/emergência antes de aplicar. Sem ampliar
   a role de backup ou conceder SG write.
3. Após autorização própria, aplicar somente o plano administrativo salvo e
   validar as permissões efetivas. Manter automações congeladas durante a transição.
4. Instalar o broker/staging privado com autorização própria. Adotar a policy
   existente pelo coordenador; validar guard/TLS e journal/readback. Não substituí-la
   por um template que perca statements legítimos.
5. Gerar plan persistent pelo backend correto. Exigir que tls tenha apenas
   `actions=["forget"]`, nenhuma criação/update/delete da policy de dados e nenhum
   delete/replace de bucket ou proteção. O gate local também rejeita aliases de
   recursos de policy fora do endereço permitido backups_tls. O controller vincula
   o bucket anterior do forget ao bucket de dados esperado e os buckets anterior/
   posterior de backups_tls ao bucket de backups; valores desconhecidos falham.
6. Autorizar separadamente o apply de forget, que remove apenas o registro do
   state. **Não executar state rm nem DeleteBucketPolicy.** Validar que a policy
   real permanece igual e que o novo plan não propõe gerenciá-la.
7. Validar o broker/lease/SSM e só então publicar/ativar o fluxo integrado de
   deploy/backup. Nada disso está autorizado por este documento.

Se um plan apresentar DeleteBucketPolicy ou replace, abortar. Nunca aplicar um
plan antigo anterior ao removed block. Não compartilhar plans/state em artifacts.
Após o forget haverá um registro Terraform persistente a menos; o recurso S3
real permanece. Não inferir contagens atuais sem consultar o ambiente autorizado.

## Instalação nova e atualização normal

O persistent cria buckets/proteções/versionamento, sem a policy de dados.
Antes de disponibilizar runtime/aplicação, a etapa administrativa cria a policy
inicial TLS+guard pelo coordenador e registra ownership. Se isso falhar, parar;
nunca expor a aplicação para compensar a ausência de guard. Esse bootstrap é
procedimento administrativo separado, não um parâmetro `skip_guard` do deploy.

O workflow ordinário não pode provisionar uma instância nova sem guard existente.
Provisionamento inicial e instalação administrativa do guard exigem autorização
e procedimento próprio; o bootstrap Terraform administrativo continua independente.
Atualizações persistentes normais não incluem a policy de dados; ela só muda
via coordenador. Durante fencing, lease/registro pendente bloqueiam o deploy.

## Lease e estado de integração

O wrapper guarded_deploy mantém um RemoteDeployLease durante persistent/runtime,
conexão e Ansible/Compose. Os gates ficam antes de chamadas mutáveis e após cada
apply. A interface host_lease lê somente protocolo limitado acquire/assert/release,
vinculado à identidade, owner e nonce. EOF/falha produz intervention_required;
release é confirmado somente depois do journal durável. Sem TTL de liberação.

Não existe fábrica de canal SSM/SSH autenticado pronta neste incremento. O caminho
CLI antigo de apply/connect agora recusa ausência de lease antes de chamar AWS
ou Terraform. O workflow publicado não foi ativado/modificado: mode=plan permanece
disponível; mode=apply falhará fechado com este controller até integrar o bridge.
Isso é um bloqueio deliberado de ativação, não um deploy já homologado.

Antes de ativar guarded_deploy, concluir atestação do escopo completo (buckets,
project/environment e state/instância), rejeição de substituição da EC2 alvo,
validação independente de host key, SDK/plugin fixados e serviço supervisor.
Os testes do protocolo são mocks, não prova de SSM ou da identidade real do host.

## Rollback e emergência

Antes de forget: reverter somente código não reverte a redução IAM/guard. Rever
policy atual e journals; mudanças remotas exigem nova autorização. Após forget:
reintroduzir ownership Terraform exige import controlado do recurso EXISTENTE
em checkout de rollback com prevent_destroy, usando backup de state como evidência,
sem restaurar state antigo sobre state que recebeu mudanças legítimas. Import
e eventual alteração IAM/policy são operações futuras, não executadas aqui.

Não remover o guard permanente automaticamente. Reverter a policy só após comparar
snapshot/diff atual e resolver fence/captura; alterações de terceiros bloqueiam
o rollback automático. Nunca DeleteBucketPolicy para recuperar acesso. A role
emergencial/root exige aprovação individual e preservação das proteções TLS.
Timeout, reboot ou perda do controlador não reabrem escrita.

## Arquivos deste incremento

- Terraform: bootstrap/operational-iam.tf, variables.tf, outputs.tf,
  data-policy-authority.tf, tests/authority.tftest.hcl; persistent/main.tf.
- Controller: scripts/ci/deploy.py e scripts/ci/tests/test_deploy.py.
- Operational: policy_ownership.py, registered_authority.py, remote_lease.py,
  host_lease.py, fence_admin_api.py; tests/test_policy_ownership.py,
  tests/test_remote_lease.py e tests/test_host_lease.py.
- Este documento. Fontes preparatórias anteriores continuam não publicadas.
- phase-7-pr7-integration-decision.md recebeu indicação explícita de checkpoint
  histórico, com referência à decisão de ownership agora aprovada.

## Bloqueadores restantes do primeiro backup

Não há ainda produtor/completion operacional autorizado, collector completo de
escritores, fence PostgreSQL de novas conexões, fábrica autenticada SSM/SDK ou
supervisor instalado. Os candidatos continuam local_candidate_only. Concluir
esses adapters não depende de dar PutBucketPolicy à role de backup.
Sigkill/reboot/processos órfãos reais só em destino descartável; nenhuma injeção
de falha é permitida na origem. A private age key definitiva permanece externa.

Autorizações AWS individuais ainda necessárias: leituras de preflight; plan
administrativo; apply IAM salvo; instalação broker/guard/supervisor; adoção da
policy; plan/apply forget; permissões SSM mínimas; validação OIDC/host key; por fim
uma janela de fencing e primeiro backup identificado. PR8 continua posterior,
independente e sem Destroy da origem.

## Validações locais deste incremento

Terraform 1.14.2: fmt-check aprovado; init backend=false com lockfile readonly e
mirror local do provider AWS 6.67.0; validate aprovado nos dois roots modificados.
Testes mock somente plan: bootstrap 22 aprovados, persistent 2 aprovados.
Os 21 testes semânticos IAM foram reexecutados contra JSON mock do código atual.
Não houve conexão ao backend real nem avaliação pelo IAM Policy Simulator AWS.
Pacote operacional atual: 226 testes aprovados, sem skips. Cobertura adicional:
TLS ausente, journal adulterado, confirmação perdida, alterações legítimas de
terceiros, não liberação por interrupção e binding do bucket no plan de migração.

Regressões: contratos PR1 73, PR2 187 aprovados/2 skips Windows, PR3 71 aprovados/
3 skips Windows, sealing PR5 144, orquestração PR6 146, sandbox 59, inventário 29,
storage guard 17, instalação inicial 18 e controller 15. Os skips locais não
anulam a homologação POSIX já aceita; não representam validação do código novo
em Linux. Fsync, SIGKILL, reboot e funcionamento SSM/SDK requerem ensaio isolado.

AST Python, PHP lint, Actionlint, links locais, scan redigido de secrets e
diff-check incluem o pacote operational untracked. Nenhuma credencial real,
state ou tfvars privado é entrada dos testes. Checksums e readback não comprovam
restauração funcional, quiescência ou exclusividade administrativa remota.
