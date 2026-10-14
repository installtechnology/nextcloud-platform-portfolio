# Deploy em recuperação sem depender da EC2 anterior

## Core recuperado no destino recriado — 14/10/2026

O objetivo funcional do laboratório foi demonstrado com intervenções
administrativas: depois da remoção seletiva e da recriação já registradas, o
destino recebeu preparação Ansible e o backup real v2/SSE-S3
`20261013T014456Z-e037319c-d8ec-40c2-8710-1735a60c84f4`.

- Preparação: run 38609103468 (execução 38609103468; histórico operacional privado),
  success, commit `13eccaf2c2b1508b18c31b97ca4360934cf314d0`;
  journal released e handoff confirmado. State permaneceu inalterado, com 24
  recursos. As retomadas desta rodada não executaram Terraform apply.
- Código final do restore: `899270623f4101fcedb3c5e989a39e94df100ae8`;
  qualificação 38614440466 (execução 38614440466; histórico operacional privado)
  success, com 523 testes restore/reprovision sem falhas, erros ou skips.
- Restore: run 38615507572 (execução 38615507572; histórico operacional privado).
  O host completou download/verificação, arquivos privados, banco/roles,
  objetos, remapeamento, startup sem instalação nova e verificação/restart.
  Journal candidate_ready, sete etapas, lease released e 173 versões verificadas.
  Onze objetos fora do conjunto selecionado permaneceram intactos.
- Verificação funcional administrativa no destino: login web confirmado,
  API de usuário autenticada e download HTTP 200. Os 196 bytes corresponderam
  ao SHA-256 `6d26c6d27c34ef8a891e8be5c2bc1ed766b69414ea19519169f9dbe061003862`
  do mapa de objetos selecionados do backup. Credenciais permaneceram no host.
  Nextcloud 34.0.4, PostgreSQL e Redis healthy; installed=true, maintenance=false.
  A URL pública também respondeu HTTP 200 em status.php a partir do computador
  do operador; esse teste externo não transmitiu credenciais.
- O workflow final ficou em **failure por SSM_CLEANUP_FAILED**, depois do core
  concluído. A leitura AWS posterior confirmou zero sessões ativas no destino.
  Não declarar esse workflow como success; sua confirmação de cleanup continua
  sendo uma limitação da automação.
- Origem preservada e stopped; destino running. Deploy, backup-operational,
  restore e backup-ssm-validation foram conferidos disabled_manually ao final.
  restore_proven=false e destroy_authorized=false não foram alterados.

Intervenções: chave pública observada por SSM e vinculada ao grant, correção
sudo/chdir, preservação da receipt anterior e atualização de quatro arquivos
públicos por hashes no host vazio. Os journals de duas falhas anteriores a dados
foram preservados administrativamente sob o lock existente, sem apagar artefatos
nem simular sucesso. Uma limpeza inicial de scaffolds removeu equivocadamente o
diretório docker vazio exigido pelo executor; ele foi reposto, e essa mudança
de limpeza não foi publicada. O bloqueio original era o inventário com onze
objetos adicionais, resolvido por recuperação lógica que os preserva.

Este resultado comprova o core no ciclo realizado, com armazenamento preservado
e atuação administrativa registrada. Não comprova execução sem intervenções,
destroy genérico, recuperação após perda do bucket de origem, todos os cenários
de falha ou recuperação de observabilidade no destino. As seções seguintes
preservam o histórico de implementação e suas autorizações anteriores.

## Recuperação lógica preservando objetos adicionais

Na reutilização do bucket preservado, todos os objetos selecionados no backup
continuam obrigatórios, com versões atuais únicas, tamanhos, SSE-S3 e bytes
SHA-256 iguais às versões exatas da origem. Chaves adicionais fora do conjunto
selecionado e das ausências conhecidas permanecem intactas, sem copiar, apagar
ou sobrescrever. Uma chave conhecida como ausente não pode conter objeto atual.
O inventário completo do destino é revalidado depois das leituras para detectar
mudança concorrente, inclusive nos objetos adicionais.

Essa modalidade restaura banco, configurações e o conjunto selecionado; não
promete igualdade física de todo o bucket nem coleta de objetos não selecionados.
Não presume que os objetos adicionais sejam descartáveis. Corrupção, ausência
de objeto selecionado, versão ambígua ou diferença de bytes continuam impedindo
reutilização. A qualificação Linux exige 523 testes.

## Retomada com chave pública verificada administrativamente por SSM

Quando a console deixou de conter a fingerprint após stop/start, a retomada
pode usar a chave pública existente observada pelo administrador via SSM
autenticado. `observed_ssm_host_key` lê somente um comando já concluído, exige
CommandId/InstanceId exatos, AWS-RunShellScript, sucesso e o resultado da leitura
da chave pública configurada no sshd. Não envia comandos nem escreve na console.

A emissora inclui opcionalmente essa observação no grant existente da nova
execução. O consumidor valida o vínculo à mesma EC2 e o formato Ed25519 antes
de conectar. `ssh-keyscan` compara a chave recebida com a chave do grant;
diferença aborta, sem fallback. StrictHostKeyChecking permanece habilitado.
Grants anteriores e criação inicial mantêm o caminho de console. Não são
adicionados recursos, permissões IAM, workflows ou outro sistema de autorização.

Depois da preparação, o restore pode usar o modo existente `configured`, com
InstanceId atual e RESTORE_KNOWN_HOSTS revisados pelo administrador usando a
mesma observação SSM. `discover_prepared` mantém seu requisito de console;
não apresentar evidência SSM como evidência de console. A observação é uma
atestação administrativa da chave existente, não prova de ausência de acesso
root ou de alteração futura da chave. O SSH rejeita uma chave posterior diferente.

Esta correção precisa passar pela qualificação Linux de 521 testes e pela
instalação real. Restore e ciclo completo não estão comprovados por testes locais.

O run `38606940005` passou pela conexão e completou as tarefas Ansible, mas
falhou no handoff: o comando fazia `cd` como ubuntu em um diretório root 0700
antes de executar sudo. A correção usa `sudo -n /usr/bin/env --chdir=...` para
entrar no diretório somente após elevação. Não altera permissões do diretório.
A qualificação passa a exigir 522 testes. A receipt dessa tentativa deve ser
preservada por manutenção administrativa explícita, com host vazio, ausência de
claim e controles compartilhados livres, antes de tentar outro run; o playbook
continua sem sobrescrever evidência antiga. Journal de criação não é liberado
manualmente. Nenhum restore ocorreu nesse run.

Implementação local para revisão. Nenhum workflow foi executado, nenhum código
foi publicado e nenhuma mudança AWS foi aplicada nesta preparação. Os resultados
já registrados em [Fase 7 PR8](phase-7-pr8.md) permanecem separados dos testes
sintéticos deste novo caminho.

## Fluxo preparado

O mesmo `deploy.yml` oferece `purpose=create_restore_target`, com `mode=apply`
e `confirmation=APPLY`. Reutiliza o root `terraform/aws/restore` e os playbooks
existentes. Prepara o host vazio; não instala uma aplicação nova sobre o backup.

1. Validar branch main, Environment, quatro buckets distintos, origem, imagens,
   chaves públicas, CIDRs, sizing e configuração aprovada.
2. Obter OIDC pela role dedicada reprovision. A role normal de deploy não é
   assumida neste caminho; backup, restore e aplicação continuam sem state.
3. Esperar por até 300 segundos, somente com leituras, pelo grant administrativo
   do run/attempt exato. Configuração, SHA publicado e hash dos inputs ficam
   vinculados ao grant. Grants inválidos e falhas de rede não provocam retries.
4. Adquirir journal versionado em prefixo exclusivo do bucket de state existente.
   Escrita condicional, leitura por VersionId e conferência de conteúdo precedem
   cada transição. Não há expiração, exclusão ou force-unlock.
5. Confirmar ausência do runtime por API. Aceitar state vazio/ausente sem versões
   históricas excluídas e bucket inexistente, ou o conjunto completo dos sete
   recursos persistentes já pertencentes ao mesmo state e bucket. Nunca adotar
   recursos externos, state parcial ou runtime residual. A policy da origem
   permanece igual à revisada, sem fencing. EC2 parada não significa ausência.
6. Gerar e conferir plan salvo: criação dos recursos descartáveis conhecidos e,
   quando preservados, somente `no-op` dos sete persistentes, com identidade e
   atributos iguais ao snapshot observado. Nenhum drift não comprovado, update, exclusão,
   substituição, forget ou importação. Exigir IMDSv2, gp3 criptografado,
   boundary exata, versionamento, SSE-S3 e bloqueios públicos. Revalidar ausência
   antes do apply, com locking Terraform normal.
7. Verificar identidade EC2/EBS e abrir somente o SSH temporário /32 já previsto
   no deploy. Validar host key pela console autenticada AWS. Instalar controles
   por Ansible, sem iniciar a aplicação.
8. Manter a trava externa enquanto adquire o lease root do host. Receipt privada
   vincula commit/run/identidade; claim exclusiva impede reutilização. O helper
   usa Python do venv e repositório instalado, com sessão limitada a 900 segundos.
   EOF, sinal ou erro preservam intervenção; SIGKILL não libera o journal.
9. Confirmar SSM Online e publicar somente tags de identificação e host key
   pública. Liberar a trava externa enquanto o lease do host continua adquirido;
   só depois encerrar o lease do host. Falha exige reconciliação explícita.
10. Após autorização separada, executar `restore.yml` com
    `target_selection=discover_prepared` e backup específico ou `latest_valid`.
    Exigir exatamente um host da conta/ambiente/commit aprovado, tags e
    fingerprint confirmado novamente pela console. Nunca há fallback do ID
    antigo. O modo `configured` anterior permanece disponível.

Deploy normal continua com lease remoto antes dos applies e Ansible/Compose.
Contratos e guards anteriores permanecem; a autoridade da policy original não
é alterada por este caminho.

## Autorização e IAM

`github_reprovision=null` é o padrão: publicação não cria recursos. Opt-in exige
destino independente, boundary de restore e emissora administrativa diferente
das roles operacionais. Trust exige OIDC, audience, IDs imutáveis do repositório,
Environment, branch e nome do workflow manual existente. Proteções do Environment
continuam necessárias.

Prefixo administrativo:
`control/reprovision/<conta>/<projeto>/<environment-github>/`.

- `grants/<run>-<attempt>.json`: somente a emissora escreve, com Deny na bucket
  policy; executor somente lê. `reprovision_grant.issue()` exige identidade
  federada exata, não sobrescreve históricos e verifica bytes/versão.
- `lease.json`: somente emissora e role de criação escrevem. Administradores
  externos/root continuam fora da exclusão absoluta; qualquer versão divergente
  observada bloqueia a operação.
- State de destino em chave própria do root restore; DeleteObject limitado ao
  seu .tflock nativo. Não há exclusão de journal, grant ou state.

Inline mantém recursos/tags/actions exatos. Boundary compacta limita APIs EC2
à região e mantém controles IAM; permissões efetivas são a interseção das duas.
Denies preservam origem/backups, administração do state, grants e dados. A
renderização sintética resultou em 5.653 bytes de boundary e 9.079 de inline.
Preconditions rejeitam excesso dos limites IAM; nomes/configurações reais ainda
precisam passar no plan administrativo autorizado.

Restore descobre destinos por tags somente com opt-in. SSM permanece restrito
ao projeto/ambiente independente e AWS-StartSSHSession, com término apenas da
sessão federada da própria identidade. Backup e Instance Profile original não
recebem acesso ao state.

## Falhas e limites

Falha de criação, apply parcial, instalação, SSM, grant, CAS ou handoff mantém
journal/recursos para diagnóstico. Não apaga, reverte ou escolhe outro destino
automaticamente. Novo run exige novo grant; não substitui operação interrompida.
Recuperação administrativa exige revisão do state e das duas autoridades; não
existe helper de desbloqueio neste patch.

Este caminho cria um host independente novo: não adota bucket nem state parcial
externo. Também aceita o armazenamento persistente completo do mesmo state,
depois de remoção autorizada dos recursos descartáveis. Para outro destino,
revisar nomes/configuração. O root restore
preserva seu bucket e prevent_destroy. Não implementa Destroy nem comprova ainda
recriação completa após Destroy. Observabilidade é descartável pelo contrato;
os volumes Grafana/Prometheus/Loki não são restaurados do backup e precisam ser
recriados pelo código normal em etapa autorizada.

### Recriação preservando armazenamento — adaptação local

O mesmo root/state/workflow é mantido. Os sete recursos preservados são
`terraform_data.isolation`, `aws_s3_bucket.data`, public access block, ownership,
versionamento, SSE-S3 e `aws_s3_bucket_policy.tls`. `prevent_destroy` e
`force_destroy=false` permanecem intactos. Nenhum comando de Destroy, import,
state rm, migração de backend ou ativação foi acrescentado.

O preflight exige conta, região, Project/Environment, identidade do bucket,
TLS, ACL BucketOwnerPreferred, bloqueio público, SSE-S3 e versionamento ativos.
Lê a policy real e compara com a policy preservada no state. O snapshot fica
vinculado à versão S3 e ao SHA-256 dos bytes do state; é revalidado imediatamente
antes do apply, além do locking e validação de serial próprios do Terraform.
Mudanças administrativas legítimas também bloqueiam e exigem revisão, nunca são
sobrescritas automaticamente. Dados, objetos e versões não são acessados pelo
criador do host.

O refresh pode reconhecer somente três metadados de `aws_s3_bucket.data`,
sempre com o valor anterior exatamente igual ao snapshot observado:

- `policy`: espelho igual à policy de `aws_s3_bucket_policy.tls`, confirmada no
  S3 pelo preflight.
- `tags`: somente normalização de `null` para `{}`; `tags_all` não pode mudar.
  O preflight exige que as tags reais correspondam integralmente a `tags_all`
  do state, sem chaves duplicadas ou falha de leitura.
- `versioning`: somente o espelho antigo `enabled=false` para `enabled=true`,
  de acordo com o recurso separado `aws_s3_bucket_versioning.data`. O valor
  booleano de MFA delete deve permanecer igual e corresponder ao recurso
  separado; status e MFA delete são conferidos novamente pela API S3.

Nenhum segundo recurso ou outro atributo pode apresentar drift. Todos os
resource_changes persistentes continuam estritamente `no-op`. Isso atualiza
metadados no state, sem modificar tags, versionamento ou policy na AWS.
Permissões, TLS, statements e outras diferenças continuam bloqueadores.
Uma tentativa rejeitada mantém o journal de criação em `intervention_required`;
o patch não reconcilia nem libera esse journal automaticamente e não autoriza
outro dispatch.

### Encerramento administrativo antes do apply

Uma criação rejeitada antes do apply pode ser reconciliada pela emissora
administrativa existente. Não se marca handoff como concluído e não se apaga,
reescreve ou libera o journal antigo. A emissora publica, por escrita condicional,
um comprovante único em `grants/reconciliations/<run>-<attempt>.json`, dentro do
prefixo já protegido pela bucket policy contra escritas das roles operacionais.
Nenhuma permissão IAM, workflow ou bucket novo é necessário.

O comprovante vincula conta/configuração, run/attempt/SHA encerrado com falha,
VersionId/hash exatos do lease e VersionId/hash/serial/lineage do state preservado.
O produtor exige observações administrativas do GitHub e das APIs AWS, com o
conjunto completo dos sete recursos persistentes e ausência de EC2, redes,
discos, key pairs, role e Instance Profile descartáveis. Flags booleanas não
substituem inventários. Falha de leitura, concorrência, recursos residuais ou
state divergente impedem a escrita; timeout não dispara nova escrita ou rollback.

Uma nova tentativa exige outro grant. O executor valida novamente o comprovante
único e lê o state real ao adquirir o lease e imediatamente antes do apply.
O grant antigo não pode ser reutilizado. O journal anterior permanece no histórico;
o registro da nova tentativa só é escrito por CAS após todas as validações.
Criações com host já vinculado continuam bloqueadas para essa modalidade.

Essa evidência é uma atestação administrativa, protegida pela autoridade exclusiva
existente; não é prova de restauração, backup ou ausência de intervenções root.
As leituras e a reconciliação não autorizam outro dispatch por si só.

Validação local da correção: 447 testes de restore executados, 444 aprovados e
três skips POSIX no Windows; 22 testes novos cobrem a reconciliação, sem serviços
externos. A futura qualificação Linux exige os 447 sem skips. Timeout, estado
alterado, receipt divergente, recursos residuais e reutilização do run falho
continuam bloqueadores. Esses testes não substituem as observações administrativas
reais exigidas antes da emissão do comprovante.

Na correção local desses metadados, a suíte de restore executou 425 testes:
422 aprovados e três skips POSIX no Windows. Os dez testes do harness também
passaram. O harness passa a exigir 425 testes sem skips na futura execução
Linux autorizada. A reprodução privada do plan rejeitado passou no guard
corrigido com 17 criações e sete `no-op`; nenhum apply foi executado nessa
validação. Leituras S3 confirmaram tags e versionamento contra o state.
Esses resultados não comprovam a recriação real nem liberam a tentativa
interrompida. O journal continua preservado, exigindo reconciliação administrativa
antes de qualquer outro workflow.

O grant novo identifica somente a EC2 nova. Tags adicionais persistentes são
reutilizadas sem alterar bucket/policy; não se propaga a versão nova do grant
pelas default_tags do provider. Um plano somente no-op dos 24 recursos não é
uma recriação válida: o runtime deve estar ausente e todos os seus recursos
devem ser criados.

**Limite desta autorização:** a EC2 atual, mesmo parada, continua bloqueando
esse caminho. Os 24 recursos atuais não foram removidos. A futura remoção
seletiva do runtime requer plano específico, verificação do backup e autorização
separada. Nunca executar `terraform destroy` sobre o root restore inteiro.

O restore continua exigindo banco, volumes e instalação vazios no host novo.
O bucket pode estar vazio ou conter exatamente o conjunto atual selecionado
no backup. Neste segundo caso, confere inventário completo, uma única versão
atual por chave, ausências, tamanhos, SSE-S3 e SHA-256 em streaming dos bytes das
versões exatas na origem e no destino. Revalida o inventário depois das leituras.
Reutiliza os VersionIds existentes sem copiar, apagar ou sobrescrever objetos;
versões históricas das chaves conhecidas permanecem intactas. Conjunto parcial,
chaves extras, corrupção, versão atual ambígua, delete marker incompatível ou
mudança observada bloqueiam antes da restauração do banco e são verificados
novamente na etapa de objetos. Não há merge, reparo automático ou fallback.

Igualdade de inventários não prova ausência de escritores. O host independente
deve estar vazio, sem aplicação antiga, com identidade e lease verificados;
operações externas continuam proibidas durante a janela autorizada. A versão
original da EC2 não pode continuar como escritora deste bucket. Ações root ou
administrativas externas continuam um limite, não uma garantia resolvida por
checksums. Esta adaptação local não concede autorização operacional nem prova
restauração funcional do ciclo novo.

#### Resultado desta adaptação local

- Restore/reprovision: 407 testes executados, 404 aprovados e três skips POSIX
  no Windows. O harness Linux foi atualizado para exigir exatamente 407, sem
  skips; essa versão ainda não foi executada no runner Linux.
- Harness de qualificação: dez testes aprovados. Regressões IAM criação:
  23 testes aprovados com a fixture Terraform offline já existente; nenhuma
  alteração de policy ou boundary neste patch.
- Root restore: validate na cópia sem backend e seis cenários Terraform mock
  aprovados, incluindo preservação dos recursos S3 e grant só na EC2 nova.
  O apply/teardown destes testes é sintético, sem AWS. O targeting do teste serve
  exclusivamente para construir a fixture de sete recursos persistentes; não
  é um comando operacional de Destroy nem autorização para manipular state.
- Actionlint, Python/HCL, links, scan de secrets, fmt e diff-check aprovados.
- Nenhuma operação AWS, dispatch, instalação, commit ou push nesta adaptação.
  Nada em staging; base HEAD/origin/main local permanece `c772682`.
- A EC2 existente continua bloqueando recriação. Publicação, qualificação Linux
  deste patch e qualquer remoção seletiva/execução real exigem autorização.

Escopo exato: 14 caminhos (13 modificados e um novo):

- `M` — `docs/phase-7-reprovisioning.md`
- `M` — `scripts/backup/qualification/tests/test_restore_linux.py`
- `M` — `scripts/restore/destination.py`
- `M` — `scripts/restore/objects.py`
- `M` — `scripts/restore/plan.py`
- `M` — `scripts/restore/qualification.py`
- `M` — `scripts/restore/reprovision.py`
- `M` — `scripts/restore/reprovision_operations.py`
- `M` — `scripts/restore/tests/test_primitives.py`
- `M` — `scripts/restore/tests/test_reprovision.py`
- `M` — `scripts/restore/tests/test_reprovision_operations.py`
- `M` — `terraform/aws/restore/runtime.tf`
- `M` — `terraform/aws/restore/variables.tf`
- `??` — `terraform/aws/restore/tests/retained.tftest.hcl`

Workflows, IAM/bootstrap, roots persistent/runtime originais, contratos PR1,
guards PR3, Ansible e Compose operacional permanecem sem diff.

## Validação e ativação pendente

Testes locais cobrem grants, histórico, concorrência, CAS ambíguo, state parcial,
host parado, plano adulterado, proteções S3/IMDS/IAM, handoff, EOF, timer,
descoberta múltipla e host key divergente. Adaptadores usam mocks; isso não
comprova provisionamento, Docker, SSM ou Linux real deste novo caminho.

Os quatro cenários Terraform usam provider mock em cópia sem backend. O apply
desses testes é exclusivamente sintético. Fixture IAM renderizada offline pelo
provider instalado, com conta/nomenclatura fictícias, sem credenciais reais.
Os testes semânticos avaliam inline/boundary; não são AWS IAM Policy Simulator.

Antes do uso: revisar/publicar com autorização; qualificar Linux e syntax-check
Ansible; revisar plan bootstrap e autorizar separadamente opt-in IAM/policy do
state e configurações privadas. Conferir a boundary da emissora existente:
policy inline nova não supera boundary que negue o prefixo administrativo.

Depois, autorizar um dispatch de criação, conferir run/SHA e emitir o grant
exato durante a espera. Confirmar destino preparado antes de autorizar restore.
Não destruir a origem nem máquinas atuais para testar código não homologado.


## Resultado local e escopo para revisão

- Restore: 377 testes executados, 374 aprovados e três skips POSIX.
- Controlador/lease/supervisor: 126 executados, 123 aprovados e três skips POSIX.
- IAM criação: 22 testes semânticos aprovados; IAM restore: 27 aprovados.
- Terraform offline: quatro cenários de criação, seis de IAM restore e quatro
  do root de destino aprovados. Plan sintético do root existente aceito pelo
  novo guard, com tags fictícias explicitamente fornecidas pelo mock.
- Terraform validate/fmt, Actionlint, sintaxe Python/YAML/HCL, links, Mermaid,
  scan de secrets e diff-check aprovados. O teste recovery.tftest.hcl recebeu
  somente formatação para atender ao fmt recursivo; sem mudança semântica.
- Nenhum teste deste caminho novo executou Linux, Ansible remoto, SSM, Docker
  ou AWS. Os seis skips e o syntax-check Ansible real continuam pendentes.
- Sem staging, commit ou push. HEAD e referência local origin/main permanecem
  na mesma base; não foi feito fetch ou consulta remota nesta preparação.

São 33 caminhos locais, incluindo as quatro documentações anteriores preservadas.
Status M significa modificado; ?? significa novo, ainda não rastreado:

- `M` — `.github/workflows/backup-pr7-linux-validation.yml`
- `M` — `scripts/restore/qualification.py`
- `??` — `scripts/backup/qualification/tests/test_restore_linux.py`

- `M` — `.github/workflows/deploy.yml`
- `M` — `.github/workflows/restore.yml`
- `M` — `README.md`
- `M` — `docs/README.md`
- `M` — `docs/phase-7-pr8.md`
- `M` — `docs/portfolio-evidence.md`
- `M` — `scripts/backup/operational/oidc.py`
- `M` — `scripts/ci/deploy.py`
- `M` — `scripts/restore/runner.py`
- `M` — `scripts/restore/tests/test_deployment.py`
- `M` — `scripts/restore/tests/test_host_preparation.py`
- `M` — `terraform/aws/bootstrap/restore-iam.tf`
- `M` — `terraform/aws/bootstrap/state.tf`
- `M` — `terraform/aws/bootstrap/tests/recovery.tftest.hcl`
- `M` — `terraform/aws/bootstrap/variables.tf`
- `??` — `ansible/playbooks/creation-handoff-prepare.yml`
- `??` — `docs/phase-7-reprovisioning.md`
- `??` — `scripts/restore/creation_lease.py`
- `??` — `scripts/restore/discover_target.py`
- `??` — `scripts/restore/initial_lease.py`
- `??` — `scripts/restore/reprovision.py`
- `??` — `scripts/restore/reprovision_grant.py`
- `??` — `scripts/restore/reprovision_operations.py`
- `??` — `scripts/restore/reprovision_runner.py`
- `??` — `scripts/restore/tests/test_reprovision.py`
- `??` — `scripts/restore/tests/test_reprovision_handoff.py`
- `??` — `scripts/restore/tests/test_reprovision_operations.py`
- `??` — `terraform/aws/bootstrap/reprovision-iam.tf`
- `??` — `terraform/aws/bootstrap/tests/reprovision.tftest.hcl`
- `??` — `terraform/aws/bootstrap/tests/test_reprovision_iam.py`

### Qualificação Linux preparada, ainda não executada

O workflow existente `backup-pr7-linux-validation.yml` permanece exclusivamente
manual, com `confirmation=VALIDATE_PR7_LINUX`, branch main e `contents: read`.
Não utiliza Environment, OIDC, Secrets ou conexão ao host. O runner hospedado
tem acesso à internet para obter as dependências fixadas; não é air-gapped.

O lock/provenance já aprovado fornece 28 wheels para qualificação. Não foram
alterados hashes, versões ou URLs; a instalação continua offline após conferir
os bytes. Nenhum download ou instalação ocorreu nesta preparação local.

O harness foi atualizado de 239 para exatamente 377 testes. Executa a suíte
restore/reprovision como root no runner descartável, com `env -i`, HOME/TMPDIR
privados e Python explícito do venv. Isso permite executar os três testes reais
de permissões root existentes, sem desativar seus guards. Qualquer erro, falha,
skip, expectedFailure, unexpectedSuccess ou contagem divergente reprova.
As mensagens mostram identidade do teste e classe da exceção, sem valores
privados. Os 18 testes locais de harness/workflow PR7/PR8 passaram.

O novo playbook creation-handoff-prepare.yml foi incluído no syntax-check, sem
execução de tarefas. Permanecem no mesmo workflow os três testes de kernel PR7,
os cinco POSIX e os syntax-checks anteriores. O teste de qualificação do harness
fica fora da suíte de restore para não alterar sua contagem de 377.

Após autorização para publicar e uma autorização de execução, o comando é:

```text
gh workflow run backup-pr7-linux-validation.yml --ref main -f confirmation=VALIDATE_PR7_LINUX
```

Conferir primeiro SHA publicado e ausência de runs concorrentes. Executar uma
única vez, sem retry automático, com resultado success de todos os steps,
377 testes de restore e zero skips, três testes kernel e cinco POSIX aprovados,
e os syntax-checks. Essa execução não autoriza IAM, provisionamento ou Destroy.

## Qualificação concluída e preparação IAM real

O commit `8e75398524de3beba790f20f2e8cfd88d260c8cc` foi publicado e o
run 38471495246 (execução 38471495246; histórico operacional privado)
passou em Ubuntu 24.04, CPython 3.12.3: 377 testes restore/reprovision, três
de kernel e cinco POSIX, com zero falhas e skips. Os quatro syntax-checks Ansible
passaram. Isso qualifica os testes isolados; não comprova criação real deste
novo destino. Deploy, backup operacional e restore permanecem desabilitados.

Na preparação posterior, o primeiro plan bootstrap real foi bloqueado pelo
limite IAM com os nomes reais. A correção local omite somente Sids opcionais
das policies inline/boundary da criação. Actions, Resources, Conditions, Effects
e Denies permanecem idênticos; Sids obrigatórios dos guards de bucket policy
não são alterados. O plan regenerado resultou em boundary de 5.706 caracteres
e inline de 8.933, dentro dos limites. Passaram quatro cenários Terraform com
mocks e 23 testes semânticos IAM, incluindo a compactação sem ampliar permissões.

O plan salvo propõe quatro criações (role dedicada, boundary e duas inline
policies) e três atualizações (inline/boundary de restore e policy do bucket de
state). Nenhuma exclusão ou substituição. TLS e os statements existentes do
bucket de state foram preservados. O refresh observado contém somente tags
vazias e o espelho computado de inline policies já gerenciadas, sem mudança
de permissões fora do escopo. O serial/lineage do state permaneceram iguais.

A emissora administrativa existente tem Deny explícito para APIs diferentes de
STS. Adicionar Allow em outra inline policy não supera esse Deny. Foi preparada
privadamente uma proposta para excepcionar somente GetBucketPolicy,
GetBucketVersioning, ListBucketVersions, GetObject, GetObjectVersion e PutObject
no prefixo administrativo de criação, mantendo as permissões/trust STS anteriores
e negando S3 fora do escopo. Doze verificações positivas/negativas passaram.
Não autoriza acesso a arquivos Terraform, outros buckets, objetos da aplicação,
DeleteObject, DeleteObjectVersion, PutBucketPolicy ou administração IAM.

Essa proposta altera uma policy administrativa existente que foi criada fora
do Terraform. Não está embutida silenciosamente no plan bootstrap: precisa de
autorização específica e comparação exata do hash/identidade antes da mudança.
Planos, snapshots e proposta ficam em diretório privado ignorado pelo Git; não
foram publicados. Nenhum apply ou alteração de permissões foi executado.

Próxima ativação autorizada: publicar a compactação testada; revisar/atualizar
somente a policy da emissora; regenerar e confirmar o plan bootstrap; aplicar
somente os quatro creates/três updates autorizados. A validação pós-ativação
deve confirmar permissões e guards sem executar criação, backup ou restore.
A criação ainda exige destino comprovadamente novo/vazio, conforme os limites
descritos acima; não substitui automaticamente o destino restaurado existente.

## Transferência do código revisado — correção local

A primeira tentativa do run 38511985579 foi cancelada enquanto o Ansible
copiava recursivamente o export Git. A cópia agora usa um único arquivo tar
produzido exclusivamente desse export, sem links ou arquivos especiais.
O destino verifica o SHA-256 antes de extrair; arquivos continuam root:root
0600 e diretórios 0700. A alteração não inicia a aplicação nem executa restore.

A segunda tentativa terminou na autorização administrativa, antes da instalação.
O journal da primeira permanece host_bound, sem handoff verificado. Este patch
não libera o lease, não modifica o journal e não autoriza uma nova tentativa.
A preparação interrompida exige reconciliação administrativa explícita; o
procedimento de abort-before-apply não se aplica porque a EC2 já foi criada.

## Retomada da preparação no mesmo deploy

O mesmo `purpose=create_restore_target` agora pode continuar a instalação
interrompida antes do handoff, sem criar outra EC2 ou aplicar Terraform.
A emissora existente usa `issue(..., observer=ResumeObserver(admin_s3))`:
o leitor administrativo consulta o state, e o coletor confere o run atual,
a tentativa anterior e todos os seus jobs pelo GitHub. Uma tentativa ainda
queued/in_progress, resposta parcial ou falha de leitura impede a autorização.
Não há novo workflow, input de desbloqueio ou permissão IAM.

O grant protegido vincula a versão do journal anterior, sua identidade,
o estado terminal do workflow e a versão/SHA-256 exatos do state. O worker
confere novamente esses vínculos, os 24 recursos esperados, a EC2 e o grant
original gravado nos recursos. A aquisição usa compare-and-swap e preserva
o histórico. O plan precisa conter somente no-op nos recursos; ele é conferido
antes da abertura da regra SSH temporária. Drift é rejeitado, exceto os campos
computados estritamente comprovados descritos abaixo.
O ramo de retomada nunca chama apply, destroy, import ou state rm.

Antes de qualquer role Ansible, o host confirma IMDSv2, ausência de aplicação
e volumes Docker, processos de preparação/banco, journals e locks ativos.
Processos antigos não são encerrados automaticamente. Recibos de handoff ou
claims já existentes continuam bloqueando este caminho: uma interrupção
depois da entrega do controle exige a recuperação do lease correspondente,
e não é tratada como uma simples falha de cópia.

Falhas sucessivas antes do handoff podem receber grants para novos runs:
a identidade original dos recursos permanece a mesma, sem alteração de tags
pelo Terraform. O novo journal conserva o destino e fica intervention_required
se a instalação falhar. Não há liberação por tempo decorrido, exclusão de
evidências ou afirmação de backup/restore concluído. A validação local utiliza
clientes sintéticos; a instalação retomada e o novo caminho POSIX ainda
precisam de qualificação/execução especificamente autorizadas.

Após stop/start, o endereço público é resolvido pela API da mesma EC2;
o ID, o EBS e a verificação independente da host key continuam obrigatórios.
O state não é editado para ajustar o IP. O plan pode refletir somente IP/DNS
públicos comprovados pela API e atualizar o output public_ip correspondente.
Espelhos computados ingress/egress do Security Group são aceitos somente
quando reproduzem exatamente as regras individuais já registradas no state.
Novas regras, CIDRs diferentes, alterações de AMI/identidade e qualquer
ação planejada de criação, alteração, substituição ou exclusão bloqueiam
a retomada. Essa exceção não concede permissão de Terraform apply.
Antes do refresh, a retomada reutiliza o cleanup existente de regras SSH
temporárias antigas, restrito às tags de CI e ao prazo conservador existente.
Os CIDRs administrativos permanentes não são revogados. A regra temporária
do novo runner continua sendo aberta somente depois da validação do plan.

O run 38581467180 terminou antes do Ansible: o plan tinha 24 recursos no-op,
mas sete registros de drift computado. A reprodução privada confirmou os
valores antes com o state e as policies inline com os dois recursos já
gerenciados. A validação reconhece tags null → {} somente em role, profile e
key pair; policies inline somente se nomes e documentos forem exatamente os
gerenciados; o espelho de rotas somente se reproduzir os recursos aws_route.
A ACL padrão inicialmente vazia é um identificador implícito computado pelo
provider: aceita-se seu preenchimento somente para a VPC original, cujo owner,
ID e CIDR são confirmados por DescribeVpcs, já autorizado. Não se afirma
inspeção das regras da ACL nem se introduz DescribeNetworkAcls na role.

A correção foi aplicada localmente ao próprio plan real rejeitado e passou,
sem apply, edição de state ou novo dispatch. Permissões diferentes, policies
adicionais, rotas diferentes, tags não vazias e qualquer ação mutável continuam
rejeitadas. O journal da tentativa encerrada permanece intervention_required;
uma nova execução exige outro grant explícito, preservando esse histórico.
