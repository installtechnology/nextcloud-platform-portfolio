# PR7 — integração local para revisão

Contrato de criptografia vigente: [v2 SSE-S3](phase-7-pr7-sse-s3.md), sem dependência operacional de age. Os resultados antigos registrados abaixo são históricos.

Status: código integrado localmente, **não homologado nem ativado na AWS**.
Base do checkout: `f885202680b2ee01dcde1decab5df47812a7bdb3`.
Nenhum commit, push, workflow, acesso operacional, manutenção ou backup real
foi realizado nesta etapa. A documentação de ownership aprovada permanece
válida; este documento atualiza o estado da implementação, não seus controles.

## Componentes conectados

| Componente | Implementação local | Limite da evidência |
| --- | --- | --- |
| Runner | OIDC em memória, SDK explícito, alvo EC2/SSM, plugin portátil e SSH autenticado | Nenhuma sessão AWS executada |
| Host | Dispatcher restrito, systemd transient unit, supervisor de Docker exec/PG/helpers | Testes sintéticos; sem ensaio systemd/Linux desta integração |
| Exclusão | flock, journals e leases duráveis de deploy e backup | Sem prova de resistência a reboot no host real |
| Captura | Nextcloud em manutenção, parada graciosa, HBA temporário e drain de sessões | Sem parada, dump ou leitura dos dados operacionais |
| S3 fencing | Broker administrativo separado, journal e ownership da policy | Sem aplicação de policy nem medição de propagação AWS |
| Selagem | SSE-S3 no destino, inventários via SDK, contrato v2, seis artefatos | Testes locais; sem backup real |
| Completion | Verificação dos bytes e VersionIds, marker por último | Checksum e marker não comprovam restauração funcional |
| Deploy | Um lease contínuo antes dos dois applies e durante Ansible/Compose | Novo caminho exige migração e instalação autorizadas |

Os adaptadores operacionais são separados dos simuladores PR3/PR5/PR6.
Nenhuma flag transforma resultado sintético em evidência operacional. O
`backup.yml` do PR6 permanece bloqueado e inalterado. O novo
`backup-operational.yml` é exclusivamente manual e depende de infraestrutura
e configuração administrativa ainda não ativadas; não está autorizado executá-lo.

## Sequência completa do backup

1. `workflow_dispatch` em main, confirmação BACKUP, Environment protegido,
   branch permitida e concurrency literalmente igual ao deploy; sem cancelamento.
2. CPython 3.12, wheelhouse com 19 wheels fixadas, bytes/METADATA conferidos,
   venv privado e instalação offline sem fallback. Plugin oficial portátil
   conferido antes de executar. O runner tem internet, não é air-gapped.
3. OIDC assume somente a role dedicada de backup. Confere conta, região,
   instância/tags/profile e SSM Agent Ubuntu 24.04 online. Usa exclusivamente
   AWS-StartSSHSession/porta 22; sem SG write, SendCommand ou shell SSM genérico.
4. SSH autentica usuário ncbackup com chave dedicada e host key independente.
   Tokens SSM ficam no ambiente privado do plugin, não no argv; credenciais AWS
   ficam em memória e no stdin criptografado do canal, nunca em arquivos/logs.
5. Dispatcher root restrito lança uma unidade transient systemd, sem daemon
   automaticamente instalado. O grupo de processos tem timeout e KillMode;
   isso não prova que execs Docker morreram. Supervisor/journals inspecionam-nos.
6. Host confere IMDSv2 de identidade, config root 0600, role temporária, buckets
   distintos, região, versionamento e SSE-S3. Lease durável cobre a operação inteira.
7. Lock comum e autoridade administrativa exclusiva são adquiridos. Confere
   operações pendentes, Docker exec, helpers, peers PG e identidade da instalação.
8. Registra manutenção original; ativa-a quando necessário; drena/paralisa
   graciosamente o Nextcloud. Audita cron, unidades, CLI, mounts e participantes
   da rede PG. HBA temporário permite somente peer postgres e bloqueia novos
   clientes; aguarda saída dos anteriores, sem matar sessões desconhecidas.
9. Só depois da drenagem aplica o Deny temporário S3. Preserva integralmente a
   policy original e a autoridade permanente. MPU pendente bloqueia: não é
   abortado silenciosamente. PutObject cobre partes e conclusão de multipart.
10. Confere continuamente os fences e escritores; inventário anterior, dump
    custom PG17/roles sem passwords, catálogo, configurações/identidade/arquivos
    mutáveis e parâmetros; inventário posterior na mesma janela e identidade.
    Inventários iguais e readback da policy jamais bastam como prova de quietude.
11. Em sucesso, restaura policy original somente com ownership e comparação
    exata, antes de reabrir PG/Nextcloud; preserva manutenção que já existia.
    Verifica restauração dos serviços. Captura permanece local_candidate_only.
12. Ainda sob lease, verifica os quatro artefatos, inventário e manifesto v2
    [SSE-S3](phase-7-pr7-sse-s3.md), sem age. Dados não passam pelo runner/GitHub.
13. Prefixo único, ausência de colisões históricas, upload HTTPS com AES256,
    multipart controlado, leitura dos bytes da versão exata, tamanho e SHA-256;
    verifica novamente disponibilidade de todas as versões de dados referidas.
14. Schema/semântica/receipts reais e referências cruzadas devem passar antes
    do completion condicional, publicado por último e também conferido por bytes
    e versão. ACK perdido exige reconciliação, nunca retry que promova ambiguidade.
15. Retorna somente resumo sanitizado; termina sessão SSM e remove temporários
    privados do runner. Nenhum artifact de dados. Backup íntegro verificado não
    significa restore_proven nem autoriza Destroy.

## Falhas e recuperação

Sem TTL de desbloqueio. SIGTERM, timeout, SIGKILL, perda do runner, broker,
conexão ou boot diferente deixam estado pendente/intervention_required. O deploy
é bloqueado mesmo quando o processo que detinha flock já morreu: os journals e
leases permanecem. Não há force-unlock, exclusão de journals, kill automático de
sessões PG desconhecidas ou restauração de policy sobre edição legítima de terceiros.

A retomada após falha é administrativa e explícita: preservar os journals;
conferir operação/boot/owner/exec IDs/sessões/helpers; demonstrar que não existem
escritores ativos; comparar política e HBA contra original e intent; resolver
ACKs ambíguos por leitura; restaurar somente mudanças pertencentes à operação;
conferir serviços/manutenção original; só então reconciliar lease. Os módulos
oferecem primitivas, mas não há comando SSH de recuperação irrestrita. Reboot,
SIGKILL e ENOSPC devem ser ensaiados em destino descartável, nunca na origem.

Root, administradores externos e poderes equivalentes a Docker continuam fora
da garantia de exclusão do usuário comum. Exige-se freeze administrativo da
janela e reconciliação se houver intervenção. A policy restringe sua edição às
autoridades admin/emergência; root da conta exige procedimento emergencial
autorizado. Hash identifica divergência, não substitui autoridade exclusiva.

## Dependências e procedência

- SDK boto3/botocore 1.40.76, Docker SDK 7.1.0; closure de 19 wheels com versões,
  nomes, tamanho, SHA-256, plataforma e dependências registrada em provenance.
  Auditoria local dos metadados passou. As wheels novas **não foram baixadas**;
  seus bytes serão verificados na preparação autorizada.
- Session Manager Plugin 1.2.707.0, pacote oficial versionado, sem instalador ou
  scripts remotos. O código oficial admite sessão via variável de ambiente; o
  canal rejeita configuração global de logging. O SHA-256 independente/revisado
  do pacote Linux ainda precisa ser obtido e aprovado. Não há declaração de
  assinatura autenticada nem de bytes já verificados. Ausência do digest bloqueia.
- SSE-S3 exige AES256 verificado em cada versão armazenada; nenhum executável
  age, recipient ou chave privada faz parte da instalação operacional.
- Host Ubuntu 24.04, CPython 3.12/venv, Docker Engine, systemd e sudo/visudo;
  runner requer CPython 3.12/venv/pip, dpkg-deb e OpenSSH. Dependência ausente
  falha fechada; não presume instalação automática ou usa outro intérprete.
- Nextcloud 34.0.4, PostgreSQL 17.11 e Redis 7.4.8 mantêm os digests aprovados
  no Compose operacional; configuração root deve usar exatamente os pins reais.

## Migração e ativação futura — autorizações separadas

1. Após revisão, autorizar publicação do código e preparação de binários/wheels
   e configuração v2. Publicação por si só não autoriza workflow ou instalação no host.
2. Autorizar verificações remotas somente leitura: SSM Agent/saída HTTPS regional,
   Instance Profile/metadata, EBS criptografado e estado real dos três roots.
   Preflight atual identifica root EBS, não substitui DescribeVolumes administrativo.
3. Selecionar issuer admin e emergency role dedicados; revisar plan bootstrap
   real e permissões efetivas. `data_policy_authority`, `recovery_ssm` e
   `github_backup` continuam null por padrão. Apply exige autorização própria.
   Role backup sem state, SG write, PutBucketPolicy ou credenciais da role deploy.
   Guard deploy é outra role; agente recebe somente canais SSM mínimos aprovados.
4. Autorizar instalação manual via recovery-prepare.yml com código exportado
   limpo/revisado, wheels/binário aprovados e config privada. Instala ncbackup,
   forced-command/sudo restrito, root staging/config, SDK offline. Não inicia
   broker, captura, manutenção ou serviço systemd automaticamente. A instalação
   do Store/journal precede a adoção administrativa da policy.
5. Exportar backup privado do state/policy anterior. Autorizar adoção da policy
   pelo coordenador admin, preservando TLS/conteúdo/proteções e registrando
   ownership. Conferir restrição permanente e caminho emergência antes do forget.
6. Gerar/revisar persistent plan: aws_s3_bucket_policy.tls deve ser **forget**
   (`removed`, destroy=false), nenhum DeleteBucketPolicy, bucket/recurso excluído
   ou substituído. Autorizar somente esse apply de migração. State passa de
   12 para 11 records; recurso S3 continua existindo. Não usar state rm/import.
   Rollback autorizado restaura ownership Terraform só com política conservada,
   fence ausente e autoridade reconciliada; nunca apply destrutivo para recuar.
7. Verificar host key por canal administrativo independente; cadastrar Variables
   públicas de alvo, roles distintas, known_hosts e digest plugin, somente a
   nova chave SSH dedicada como Secret. Restringir Environment/main/aprovadores.
   Trust backup deve usar o nome exato Backup platform (operational manual).
8. Autorizar preflight SSM/OIDC/read-only e ensaios Linux/PG/Nextcloud descartáveis
   desta integração. Falhas destrutivas nunca ocorrem na EC2 com dados. O deploy
   exige guard instalado e policy adotada; sem eles falha antes de qualquer apply.
   Instalação nova permanece procedimento administrativo de bootstrap exclusivo,
   seguido da instalação do guard, sem bypass de primeiro deploy no workflow.
9. Autorizar sessão administrativa temporária do broker root separado, com
   credenciais em memória/stdin, duração restante mínima 3000s, sem entregá-las
   ao workflow backup, ncbackup ou Instance Profile da aplicação.
10. Autorizar explicitamente **uma** janela de manutenção/backup real. Executar
    workflow manual somente após todos os critérios anteriores. Em falha,
    preservar tudo e parar, sem redispatch automático.
11. Revisar manifesto, seis versões cifradas, completion e integridade local/remota.
    PR8 continua separado: restauração funcional em destino independente, sem
    destruir origem. Nenhuma autorização de Destroy deriva deste resultado.

## Validação local

| Suíte | Resultado |
| --- | --- |
| PR7 operacional integrado | 309 aprovados, 3 novos testes Linux pulados no Windows; 312 coletados |
| PR1 | 73 aprovados |
| PR2 | 187 aprovados, 2 skips Windows/POSIX |
| PR3 | 71 aprovados, 3 skips Windows/POSIX |
| PR5 | 144 aprovados, age sintético local |
| PR6 | 146 aprovados |
| IAM semântico PR4 | 21 aprovados, fixture Terraform mock; não IAM Simulator AWS |
| Sandbox | 59 aprovados |
| CI/controller | 15 aprovados |
| Storage guard / instalação | 17 / 18 aprovados |
| Terraform bootstrap/persistent | validate offline; 25 / 2 mock runs aprovados |

Os cinco testes POSIX antigos já foram aceitos pelo operador na homologação
GitHub Actions anterior. Seus skips aqui descrevem apenas execução Windows.
Isso **não** homologa os novos adaptadores Linux, HBA, systemd, SSM, propagação
S3 ou recuperação de processos órfãos. Os testes novos comprovam caminhos e
rejeições em mocks, não consistência de um dump operacional nem restore real.

Ansible não está instalado neste executor: YAML/estrutura podem ser verificados,
mas syntax-check Ansible e ensaio da instalação ficam pendentes. Nenhuma nova
dependência foi instalada para esta entrega.

A role utiliza módulos builtin para instalar exclusivamente a chave forced-command;
não introduz dependência silenciosa de ansible.posix, ausente do lock existente.

### Comandos da futura execução autorizada (não executados)

Instalação administrativa, somente após revisar os inputs privados e o código:

```sh
ansible-playbook -i <inventario-privado> ansible/playbooks/recovery-prepare.yml \
  --extra-vars @<inputs-privados-revisados>
```

Os inputs devem incluir recovery_install_confirmation=INSTALL_RECOVERY_GUARDS,
wheelhouse aprovado, export limpo do código, configuração v2, chave pública
dedicada e configuração root ligada ao alvo real. Não contêm sessão AWS persistida.
O broker é iniciado separadamente por administrador autorizado com o Python do
venv e `-m scripts.backup.operational.admin_broker`; sua sessão é fornecida
somente por stdin protegido/in-memory, nunca como argumento ou arquivo do projeto.

Após publicação, ativação, preflight e autorização da janela:

```sh
gh workflow run backup-operational.yml --ref main \
  -f environment=lab -f confirmation=BACKUP
```

Conferir SHA aprovado de main antes do dispatch. Executar uma vez; em falha não
repetir. O resumo retorna apenas reason code, ID validado do backup e níveis de
verificação, sem conteúdo dos artefatos ou diagnóstico remoto arbitrário.

Correções concretas na integração: deny S3 passou a ocorrer após a drenagem
HTTP/PG, com restauração antes de reabrir aplicação; lease durável passou a cobrir
também criptografia/transferência; propagação das saídas Terraform ao processo
Ansible contínuo foi corrigida. A role de instalação mantém diretórios privados,
mas o PHP coletor de código não sensível recebe 0644 para que o helper www-data
possa ler seu bind individual somente leitura. Config/journals continuam privados.

Comandos locais principais: unittest discover das suítes acima; PHP lint;
bash -n; AST Python/parse JSON/YAML/HCL; Actionlint; auditoria lock/provenance;
Terraform fmt -check e validate/test em cópias sem backend com mirror local;
gitleaks sobre export exclusivo dos diffs; git diff --check e whitespace dos novos.
Não foi executado plan com backend real.

## Auditoria direcionada à publicação

Conclusão: **apto à publicação preparatória coordenada; não apto a backup real
nem a publicação com expectativa de apply imediatamente disponível**. O checkout
contém exatamente 88 arquivos (8 modificados e 80 untracked), relacionados abaixo.
HEAD e a referência local origin/main permanecem na base informada; não houve
fetch nesta auditoria nem confirmação de ausência de alterações remotas.

O deploy novo exige lease/guard, SSM e ownership administrativo antes dos applies.
Publicá-lo antes da ativação bloqueia deliberadamente mode=apply. Não é permitido
reexecutar versões antigas desprotegidas como fallback, especialmente após forget.
Recomenda-se publicar os 88 arquivos juntos, em janela aprovada de suspensão dos
dispatches: desabilitar o deploy, conferir jobs em andamento/pendentes, reconciliar
sessões antigas e aguardar expiração de credenciais que ainda permitam editar a
policy. Manter o novo backup indisponível por controle administrativo até ativação.
Só reabilitar deploy após guards, identidade, ownership, SSM e migração validados.
Não há bypass temporário. Se a ativação não for autorizada, o apply permanece
suspenso; essa indisponibilidade deve ser aceita antes da publicação.

Environment restrito a main, revisão do SHA e operadores autorizados são necessários.
Não presumir disponibilidade de required reviewers no plano GitHub do repositório
privado. A aprovação administrativa é independente das condições do código.
Os workflows são manuais; publicar não executa instalação nem backup.

Correção concreta desta auditoria: o instalador criava dependência de pip global
antes do venv. Agora cria o venv com a biblioteca padrão e executa a verificação e
instalação offline pelo seu intérprete privado. Ausência de python3.12-venv/ensurepip
continua bloqueadora; não instala pacotes automaticamente nem tenta outro Python.
Também foi corrigida a ordem de ativação acima: journal/Store instalado antes da
adoção da policy. Contratos, guards PR3 e backup.yml bloqueado permanecem intactos.

### Qualificação Linux preparada, não executada

O profile runtime conserva suas 19 wheels. O profile qualification adiciona nove
wheels de teste com versões, hashes, tamanhos e metadados públicos PyPI revisados:
ansible-core 2.19.14, PyYAML 6.0.3, Jinja2 3.1.6, MarkupSafe 3.0.3,
cryptography 46.0.3, cffi 2.0.0, pycparser 2.23, packaging 25.0 e resolvelib 1.2.1.
Closure de 28 wheels compatível com CPython 3.12/Linux AMD64. Não foram baixadas
wheels; metadados/hash publicado não significam conferência dos bytes ou assinatura.
Este profile prepara ferramentas de teste, sem selecionar captura operacional.

Execução futura, mediante autorização, em Ubuntu 24.04 AMD64 descartável, sem
OIDC, Secrets, credenciais, .aws ou acesso à origem. Runner hospedado possui internet.
Conferir CPython 3.12/venv e PHP CLI antes de começar; dependência ausente bloqueia,
sem instalação improvisada. Fornecer Config.php oficial revisado do Nextcloud 34.0.4
com SHA-256 `7cfcce8a93c6a32c337ec167c57b30d81c5bc810511bb25f812bd49416f927ad`.
Esse hash identifica a cópia revisada, não uma assinatura do publicador.

```bash
python3.12 -m scripts.backup.operational.dependencies prepare-qualification "$RUNNER_TEMP/nextcloud-operational-qualification"
PY="$RUNNER_TEMP/nextcloud-operational-qualification/venv/bin/python"
"$PY" -m scripts.backup.orchestration.posix_validation
# CAPTURE_TEST_PHP e CAPTURE_TEST_OC_CONFIG apontam para os inputs revisados acima.
"$PY" - <<'PYTHON'
import unittest
suite = unittest.defaultTestLoader.discover('scripts/backup/operational/tests')
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() and not (
    result.skipped or result.expectedFailures or result.unexpectedSuccesses) else 2)
PYTHON
cd ansible
"$PY" -m ansible.cli.playbook -i inventory/hosts.yml.example playbooks/recovery-prepare.yml --syntax-check
```

Limpar variáveis de credenciais e Python externas antes desse procedimento. O
download futuro verifica bytes/METADATA antes de instalar com no-index/no-deps e
hashes no venv; sem fallback. Nenhum workflow adicional foi criado ou executado.
Os três testes novos exercitam flock real, exclusão entre processos e SIGKILL de
um filho sintético descartável com lease durável que continua bloqueando. Não
ensaiam Docker, PostgreSQL, systemd, reboot ou encerramento na origem.
Syntax-check Ansible e esses três testes permanecem pendentes.

### Autorizações e bloqueadores do primeiro backup

1. **Somente leitura de dados:** identidade AWS, IAM/trust/boundaries efetivos,
   EC2/EBS/metadata/profile, SSM Agent e saída HTTPS, políticas/proteções S3;
   espaço EBS, integridade Nextcloud, versões PG, escritores/cron e sessões.
   Nenhuma dessas verificações remotas foi feita nesta auditoria. Um helper Docker
   criado pelo preflight tem efeito no host e não deve ser anunciado como leitura
   estrita. StartSession também exige autorização, embora não altere dados.
2. **Aprovação de material e ensaios:** host key independente, chave SSH dedicada,
   configuração SSE-S3; pacote Linux do plugin
   1.2.707.0 com digest independente aprovado. Qualificação Linux acima e ensaios
   de HBA, SDK Docker, systemd/orphans e Nextcloud/PG isolados ainda necessários;
   nenhuma falha destrutiva será injetada na origem.
3. **Mutações AWS separadas:** plan bootstrap real revisado e apply IAM específico
   de roles backup/guard/admin, boundary e canais mínimos do agente; instalação
   ou ativação SSM Agent apenas se necessário e aprovada. Sem SG write para backup.
4. **Mutações do host/GitHub:** instalação dos guards e dependências privadas,
   configuração root, forced-command; Environment/Variables/Secret SSH e suspensão
   coordenada dos dispatches. Não inicia captura nem serviço automaticamente.
5. **Mutações administrativas S3/state:** adoção da policy com ownership permanente
   preservando o conteúdo; depois plan/apply persistent exclusivamente forget-only.
   State muda de 12 para 11 records, policy real permanece. Nenhum DeleteBucketPolicy,
   state rm/import, remoção de bucket/objeto/versão. Runtime esperado mantém 15 records.
6. **Sessões e primeira janela:** autorizar SSM/SSH de verificação, broker root com
   credenciais efêmeras separadas, depois uma execução manual que altera manutenção,
   HBA e policy temporária, produz dump/artefatos e grava objetos no bucket backups.
   Confirmar espaço para os artefatos de captura e journal, MPU ausente,
   controles de escritores e espaço de journal antes da janela. Não há liberação por
   timeout, nem retry automático de backup/completion ambíguo.

Primeira ação recomendada após publicação coordenada: autorizar preflight
administrativo somente leitura para confirmar SSM/profile/saída, estado IAM/S3,
EBS e escritores reais. Em paralelo, autorizar a qualificação Linux descartável.
O primeiro backup depende desses resultados; checksums/completion não provam
restauração funcional e não autorizam Destroy. PR8 permanece independente.

### Fontes de procedência

- [venv e ensurepip no CPython 3.12](https://docs.python.org/3.12/library/venv.html).
- [Histórico oficial do Session Manager Plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/plugin-version-history.html).
- [Criptografia SSE-S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingServerSideEncryption.html).

### Referências do canal SSM

- [SSH sobre Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-getting-started-enable-ssh-connections.html).
- [Restrições de sessões próprias](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-quickstart.html).
- [Plugin oficial fixado: leitura de sessão em memória](https://github.com/aws/session-manager-plugin/blob/1.2.707.0/src/sessionmanagerplugin/session/session.go).
- [Logging padrão do plugin](https://github.com/aws/session-manager-plugin/blob/1.2.707.0/src/log/defaultconfig.go).

## Arquivos do checkout

A tabela abaixo inclui todos os arquivos modificados/untracked, sem staging.
`phase-7-sandbox-first-run-plan.md` é preparação anterior preservada, não execução
de VM nem componente obrigatório do PR7. Nenhum arquivo real de dados ou state
integra esta relação.

<!-- file-manifest: generated from local Git status, not a commit -->

| Status Git | Caminho relativo ao repositório |
| --- | --- |
| `M` | `.github/workflows/deploy.yml` |
| `M` | `scripts/ci/deploy.py` |
| `M` | `scripts/ci/tests/test_deploy.py` |
| `M` | `terraform/aws/bootstrap/backup-iam.tf` |
| `M` | `terraform/aws/bootstrap/operational-iam.tf` |
| `M` | `terraform/aws/bootstrap/outputs.tf` |
| `M` | `terraform/aws/bootstrap/variables.tf` |
| `M` | `terraform/aws/persistent/main.tf` |
| `??` | `.github/workflows/backup-operational.yml` |
| `??` | `ansible/playbooks/recovery-prepare.yml` |
| `??` | `ansible/roles/recovery_operational/tasks/main.yml` |
| `??` | `docs/phase-7-pr7-fencing.md` |
| `??` | `docs/phase-7-pr7-integrated.md` |
| `??` | `docs/phase-7-pr7-integration-decision.md` |
| `??` | `docs/phase-7-pr7-operational-path.md` |
| `??` | `docs/phase-7-pr7-policy-migration.md` |
| `??` | `docs/phase-7-pr7-ssm.md` |
| `??` | `docs/phase-7-sandbox-first-run-plan.md` |
| `??` | `scripts/backup/operational/__init__.py` |
| `??` | `scripts/backup/operational/admin_broker.py` |
| `??` | `scripts/backup/operational/backup_lease.py` |
| `??` | `scripts/backup/operational/capture_backend.py` |
| `??` | `scripts/backup/operational/completion.py` |
| `??` | `scripts/backup/operational/database_fence.py` |
| `??` | `scripts/backup/operational/dependencies.py` |
| `??` | `scripts/backup/operational/dependencies/provenance.json` |
| `??` | `scripts/backup/operational/dependencies/requirements.lock` |
| `??` | `scripts/backup/operational/deploy_plan.py` |
| `??` | `scripts/backup/operational/docker_api.py` |
| `??` | `scripts/backup/operational/drained_capture.py` |
| `??` | `scripts/backup/operational/encryption.py` |
| `??` | `scripts/backup/operational/fence_admin_api.py` |
| `??` | `scripts/backup/operational/fence_policy.py` |
| `??` | `scripts/backup/operational/fencing.py` |
| `??` | `scripts/backup/operational/host_dispatch.py` |
| `??` | `scripts/backup/operational/host_lease.py` |
| `??` | `scripts/backup/operational/host_probe.py` |
| `??` | `scripts/backup/operational/host_setup.py` |
| `??` | `scripts/backup/operational/host_supervisor.py` |
| `??` | `scripts/backup/operational/host_worker.py` |
| `??` | `scripts/backup/operational/interlock.py` |
| `??` | `scripts/backup/operational/inventory_hooks.py` |
| `??` | `scripts/backup/operational/inventory_projection.py` |
| `??` | `scripts/backup/operational/lease.py` |
| `??` | `scripts/backup/operational/live_capture.py` |
| `??` | `scripts/backup/operational/nextcloud-recovery` |
| `??` | `scripts/backup/operational/nextcloud-recovery-ssh` |
| `??` | `scripts/backup/operational/oidc.py` |
| `??` | `scripts/backup/operational/plugin.py` |
| `??` | `scripts/backup/operational/policy_ownership.py` |
| `??` | `scripts/backup/operational/postgres.py` |
| `??` | `scripts/backup/operational/proxy.py` |
| `??` | `scripts/backup/operational/read-state.php` |
| `??` | `scripts/backup/operational/receipt.py` |
| `??` | `scripts/backup/operational/registered_authority.py` |
| `??` | `scripts/backup/operational/remote_lease.py` |
| `??` | `scripts/backup/operational/runner.py` |
| `??` | `scripts/backup/operational/runtime_config.py` |
| `??` | `scripts/backup/operational/s3.py` |
| `??` | `scripts/backup/operational/sdk.py` |
| `??` | `scripts/backup/operational/ssh_channel.py` |
| `??` | `scripts/backup/operational/ssm.py` |
| `??` | `scripts/backup/operational/ssm_policy.py` |
| `??` | `scripts/backup/operational/supervision.py` |
| `??` | `scripts/backup/operational/tests/test_capture_backend.py` |
| `??` | `scripts/backup/operational/tests/test_completion_operational.py` |
| `??` | `scripts/backup/operational/tests/test_docker_api.py` |
| `??` | `scripts/backup/operational/tests/test_drained_sequence.py` |
| `??` | `scripts/backup/operational/tests/test_encryption.py` |
| `??` | `scripts/backup/operational/tests/test_fence_admin_api.py` |
| `??` | `scripts/backup/operational/tests/test_fencing.py` |
| `??` | `scripts/backup/operational/tests/test_host_lease.py` |
| `??` | `scripts/backup/operational/tests/test_integration.py` |
| `??` | `scripts/backup/operational/tests/test_interlock.py` |
| `??` | `scripts/backup/operational/tests/test_inventory_hooks.py` |
| `??` | `scripts/backup/operational/tests/test_lease.py` |
| `??` | `scripts/backup/operational/tests/test_policy_ownership.py` |
| `??` | `scripts/backup/operational/tests/test_read_state_operational.py` |
| `??` | `scripts/backup/operational/tests/test_remote_lease.py` |
| `??` | `scripts/backup/operational/tests/test_s3.py` |
| `??` | `scripts/backup/operational/tests/test_ssm.py` |
| `??` | `scripts/backup/operational/tests/test_supervision.py` |
| `??` | `scripts/backup/operational/tests/test_transport.py` |
| `??` | `scripts/backup/operational/transport.py` |
| `??` | `terraform/aws/bootstrap/data-policy-authority.tf` |
| `??` | `terraform/aws/bootstrap/recovery-ssm.tf` |
| `??` | `terraform/aws/bootstrap/tests/authority.tftest.hcl` |
| `??` | `terraform/aws/bootstrap/tests/recovery.tftest.hcl` |
