# Fase 7 — PR6: orquestração manual bloqueada e validação offline

Base local e tracking `origin/main`: PR5 `674d347`. Desenvolvimento local para
revisão, sem commit, push, dispatch, instalação de dependências ou operações AWS.
O tracking foi conferido localmente; não houve consulta remota para provar ausência
de commits posteriores. Nenhum contrato normativo foi alterado.

**A orquestração operacional está incompleta e indisponível.** O workflow de
backup é uma barreira executável que sempre recusa operação, inclusive com BACKUP
e todas as Variables presentes. Não existe input, Secret ou flag que o habilite.
Uma futura ativação exige código revisto, decisões pendentes e autorização própria.
PR7/PR8 não foram iniciados. Nenhum backup ou restauração foi realizado.

## Auditoria: fatos do código

| Componente | Implementação observada | Limite nesta entrega |
| --- | --- | --- |
| Deploy Fase 6 | Dispatch, plan/apply confirmado, OIDC, Terraform remoto, SSH temporário, Ansible e healthchecks | Operação anterior relatada e documentada; não revalidada na AWS aqui |
| PR1 | Quatro schemas fechados e relações normativas v1 | Documentos válidos não comprovam bytes, consistência ou restore |
| PR2 | Validador de documentos/artefatos locais, streaming e confinamento | Cinco ensaios POSIX PR2/3 continuam pendentes; sem verificador S3 real |
| PR3 | Lock, journal durável, manutenção com ownership, dump custom e arquivos protegidos | CLI/DockerBackend aceitam somente fixture local Linux/root `nc-capture-test-*`; nenhum adapter operacional |
| PR4 | Role, inline policy e boundary opt-in, OIDC separado de deploy | `github_backup=null`; não ativado nesta entrega, sem homologação OIDC real |
| PR5 | age público, inventário pareado e protocolo de transporte/conclusion | MemoryS3, InventoryHooks, Transport e SimulatedSeal exigem simulação; sem cliente AWS/attestor real |
| PR6 | Workflow bloqueado, guards, checklist sintético e harness POSIX | Nenhum coletor remoto, SSH, dump, transporte ou completion operacional |

Compose fixa Nextcloud 34.0.4 Apache, PostgreSQL 17.11 Bookworm e Redis 7.4.8
por digest. Volumes relevantes: `nextcloud_html` e `postgres_data`; config em
`/var/www/html/config`. Não existe serviço cron separado no Compose, mas isso não
comprova ausência de cron/CLI/jobs externos no host. Ansible prepara secrets locais
e instalador idempotente, sem integrar captura ao deploy.

Bootstrap contém state/OIDC/deploy, boundary EC2 e policy backup_restore sem
attachment. A role PR4 tem boundary própria e não reutiliza backup_restore.
Persistent mantém data/backups separados, versionamento, SSE-S3 AES256, TLS e
bloqueio público, sem expiração/force_destroy. Runtime mantém EC2/Instance Profile
limitado a dados, IMDSv2 required/hop=2 e SSH somente nos CIDRs administrativos.
Nenhuma configuração privada, state ou tfvars operacional foi lida para este PR.

## Decisão de conectividade necessária

O runner Ubuntu hospedado comum tem saída variável. A role PR4 autoriza descoberta
regional EC2 e APIs S3 restritas; não autoriza SG write, GetConsoleOutput, SSM,
Instance Connect, IAM, state ou provisionamento. Não reutilizar a role de deploy,
suas regras temporárias, credenciais ou controller `connect`.

| Alternativa | Segurança / compatibilidade | Custo e decisão necessária |
| --- | --- | --- |
| Runner hospedado maior com egress estático | SSH de faixa previamente aprovada, sem SG write na role de backup; host key pin independente | Cobrança/plano GitHub e faixa de IP; mudança SG administrativa separada, disponibilidade não verificada |
| Runner efêmero dedicado fora da EC2 da aplicação, com egress fixo | Escopo por repositório/Environment, sem acesso de PRs não confiáveis; mesma condição de SG/host key | Host, atualização, isolamento e saída fixa a administrar; não instalar runner na aplicação |
| Runner isolado na VPC / rede privada roteada | SSH privado de origem controlada, sem exposição pública adicional | Nova arquitetura de runner, SG de origem, conectividade GitHub e custo; aprovação separada |
| SSM | Dispensa SSH de entrada se agente e conectividade forem preparados | PR4 nega execução remota; agente/role/endpoints e IAM exigem decisão específica |
| Runner de operador na origem administrativa já autorizada | Pode aproveitar o CIDR existente somente se origem e custódia forem comprovadas | Disponibilidade/cancelamento/isolation do computador e chave; runner dedicado e aprovação, não presumir acesso por OIDC |

Recomendação para avaliação no laboratório: saída fixa previamente autorizada,
runner isolado e host key Ed25519 entregue por canal administrativo independente,
vinculada à identidade da instância e com rotação revisada. Isso não é uma escolha
aplicada. Não há autorização neste PR para alterar SG, IAM ou criar runner.
[Runners maiores e IP estático](https://docs.github.com/en/actions/reference/runners/larger-runners).

O deploy autentica fingerprints via API EC2 GetConsoleOutput e só então aceita
ssh-keyscan. A role de backup não tem essa API; não copiar esse fluxo nem confiar
em keyscan sozinho. Se escolhido SSH, usar pin independente, StrictHostKeyChecking,
BatchMode, IdentitiesOnly, chave dedicada e temporária no runner; nenhum fallback
TOFU ou host key checking desativado. Nenhuma chave é configurada por este PR.

Há também uma decisão de colocação do transporte: plaintext deve permanecer no
host de captura protegido, ser cifrado ali e somente ciphertext chegar ao S3.
A Instance Role atual não acessa backups e não será ampliada. Uma futura sessão
OIDC de backup no processo remoto exige protocolo revisto de entrega em memória
sobre canal autenticado, isolamento, duração/renovação e limpeza, sem argv/arquivos
persistentes/console. Alternativas a essa entrega exigem outra decisão; nenhuma
credencial é transmitida ou adapter criado aqui. Não mover dump/config ao runner.

## Workflow e limites de autorização

[backup.yml](../.github/workflows/backup.yml) usa somente workflow_dispatch,
Environment parametrizado (default lab), input BACKUP e nome literal
`Backup platform (manual)`, igual ao exemplo PR4. Contents read é a única
permissão do token; não concede id-token, não injeta Secrets, não instala ferramentas
e não publica artifacts/cache/summary. Action de checkout fixada por SHA,
ref igual a github.sha e persist-credentials=false.
Os inputs passam por env, nunca por interpolação no comando shell.

Antes do checkout, Bash exige literalmente workflow_dispatch, refs/heads/main e
BACKUP (VALIDATE_POSIX no workflow de testes). Essa comparação é sensível à
capitalização, independentemente da comparação de expressões do GitHub. Variables
não podem selecionar outra ref para checkout. O guard Python posterior exige
que DEPLOY_BRANCH corresponda à ref; com a barreira shell, main é a única branch
aceita pelo workflow. Um Environment sintaticamente válido diferente não
habilita operação: os controles externos desse Environment não são atestados.

Guard confere evento, confirmação, branch configurada/ref exato, nome de workflow,
nome de Environment e conta/projeto necessários ao grupo de concorrência. Depois
retorna sempre exit 78 / OPERATIONAL_BACKUP_DISABLED, backup_complete=false e
functional_restore=not_verified. Contexto inválido: exit 2; erro interno sanitizado:
exit 3. Somente nomes fixos de blockers/reason codes aparecem no JSON.

O Environment deverá restringir deployments à branch configurada, revisão humana
quando disponível no plano privado e proteção de main/arquivos de workflow/código
com revisão/CODEOWNERS conforme governança aprovada. Esses controles externos
**não foram consultados/configurados nesta entrega**. Input não substitui proteção
contra quem pode editar código, duplicar nomes de workflows ou contornar revisores.

PR4 exige issuer token.actions.githubusercontent.com, aud sts.amazonaws.com,
subject exato com Environment/IDs imutáveis, repository_id/repository_owner_id,
ref e workflow StringEquals. Environment no sub não inclui automaticamente branch.
O nome de workflow não atesta digest/caminho do código. Testes verificam bindings
YAML/HCL e condições sintéticas existentes; não emitem JWT nem validam STS.
Formato clássico não ativa backup PR4, que requer IDs. Confirmar claims reais e
formato imutável esperado antes da ativação, sem imprimir token.
[Claims AWS](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_iam-condition-keys.html),
[Environment/OIDC GitHub](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).

Futuro job que assuma a role exclusiva receberá id-token:write apenas nesse job,
com identidade/conta/região e tempo de sessão conferidos. Não existe tal job no PR6.
Não configurar secrets operacionais para testar a barreira.

## Exclusão mútua e interrupções

Grupo idêntico ao deploy:
`aws-${{ vars.AWS_ACCOUNT_ID }}-${{ vars.PROJECT_NAME }}-${{ inputs.environment }}`,
cancel-in-progress=false. Não usar workflow/run_id na chave, que separaria locks.
Isso serializa jobs participantes da mesma conta/projeto/Environment, incluindo
plan do deploy. Variables ausentes bloqueiam o guard; nomes devem permanecer
canônicos porque grupos são case insensitive. A fila padrão pode substituir uma
execução pendente, sem cancelar a ativa; não representa fila durável de backups.
[Concorrência GitHub](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

| Contexto | Resultado previsto |
| --- | --- |
| Mesma conta/projeto/Environment nos dois workflows | Mesmo grupo, sem cancelar a execução ativa |
| AWS_ACCOUNT_ID ou PROJECT_NAME ausente | Grupo pode ser incompleto; backup rejeita contexto antes de operar |
| Environment diferente | Grupo diferente; backup permanece bloqueado, não protege um deploy de outro Environment |
| Diferença apenas de capitalização | Grupo GitHub equivalente; guards exigem valores canônicos e rejeitam contexto divergente |

A expressão foi comparada literalmente e os casos foram modelados em testes;
nenhuma execução concorrente real ou comportamento do scheduler foi ensaiado.

Concurrency não controla operadores, outros repositórios ou processo remoto órfão.
Após perda/cancelamento do runner, o grupo pode liberar enquanto captura continua.
**Deploy atual não inspeciona journal PR3 antes de modificar a stack.** Antes de
operar, exige revisão de um guard compartilhado de journal/lease/lock e supervisão
local durável; nenhuma mudança no deploy ou unidade ativa é feita aqui. Não basta
adicionar always()/trap no runner. O código de backup atual sequer inicia captura.

PR3 registra estado anterior/intents/fingerprint e não desliga manutenção de outra
operação. Falha/ownership ambíguo exige intervenção; recover abandona a captura e
retoma somente serviços controlados, sem novo dump. SIGKILL/reboot não executam
finally; ExecStopPost está apenas em template inativo não homologado. PR5 preserva
partials/journal, rejeita colisões/ambiguidades e não faz retry cego de conclusão.
Timeout mata cliente Docker, mas exec remoto pode sobreviver: precisa de ensaio.
Nenhuma dessas garantias mockadas é recuperação operacional demonstrada.

## Preflight preparado, somente observações sintéticas

[preflight.py](../scripts/backup/orchestration/preflight.py) é função pura,
sem collector, CLI de arquivos, callback remoto ou importação de captura/transporte.
Aceita formato interno fechado de configuração/observações synthetic=true; recusa
campos adicionais, conteúdos de secrets e evidência não sintética. Não é novo
schema normativo. Também não comprova que um booleano corresponde a estado real.

Checks: observação UTC válida/recente (até 5 minutos, sem futuro), conta/região,
instância única/running/status, tags Project/Environment, profile esperado,
identidade da instalação/commit, buckets distintos, versões/AES256/TLS/bloqueio,
role de backup exclusiva/ativa, permissões revisadas, serviços installed/healthy,
Nextcloud 34.0.4/PG e pg_dump major17, manutenção inicialmente off, journal terminal,
lock disponível, fencing revisado, host key/credenciais, age/custódia, espaço e
orçamento de sessão até 3600 s. Manutenção preexistente bloqueia o futuro backup
manual conservador; PR3 continua preservando seu comportamento anterior em fixtures.

Todos os checks conformes ainda retornam operational_allowed=false,
authenticated_evidence=false e backup_complete=false. State/checksums/marker
simulado/local_candidate_only não autorizam backup nem Destroy. Coleta real,
permissões efetivas (incluindo bucket policies), TOCTOU e reconfirmações por etapa
permanecem pendentes. Nenhuma verificação de escrita remota é executada.

## Proposta Linux isolada, sem execução autorizada

[backup-posix-validation.yml](../.github/workflows/backup-posix-validation.yml)
é separado, manual, main + VALIDATE_POSIX, Ubuntu24.04, contents read, sem
Environment/OIDC/Secrets ou integração AWS/EC2/Docker. Não foi executado.
Não usa artifact/cache, instala dependências ou baixa age. O harness recusa
credenciais conhecidas, remove variáveis não necessárias durante os testes,
e exige Python>=3.11, jsonschema==4.23.0 e rfc3339-validator==0.1.4 já disponíveis.
Dependência ausente ou versão divergente falha fechada; preparar essas dependências em
imagem/ambiente descartável requer revisão/autorização antes do dispatch.

Plano de autorização: revisar workflow e dependências preexistentes, usar runner
descartável sem credenciais/arquivos AWS/rota operacional e autorizar uma execução
somente de validação. Sem download automático ou instalação local nesta entrega.
Runner hospedado tem rede e ferramentas gerais; ausência de OIDC/Secrets e código
sem clientes não equivale a air gap. Para isolamento de rede comprovado, executar
o mesmo harness em Ubuntu24.04 descartável com dependências pré-carregadas e
egress bloqueado, permitindo sockets Unix locais. Runner nunca hospedado na EC2.

### Dependências Linux: preparação proposta, não executada

O [manifesto oficial Ubuntu24.04 do runner](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)
lista Python e ferramentas gerais, mas não garante os dois pacotes Python nas
versões exigidas. A disponibilidade no runner não foi medida. O workflow atual
recusará execução se estiverem ausentes ou divergentes; não instalará nada.
Ferramentas AWS podem existir na imagem, mas não são invocadas pelo workflow.

Proposta para aprovação separada: preparar em ambiente descartável CPython3.12 /
Linux AMD64 um lock completo de dependências transitivas e hashes, mantendo os
dois pins acima. Obter somente wheels compatíveis do PyPI oficial por HTTPS,
conferir hashes publicados e revisar o wheelhouse; não usar sdists ou scripts de
instalação remotos. Hash de pacote confirma bytes, não identidade independente
do autor. Não copiar um lock Windows nem inventar hashes Linux.

Em um venv descartável, usar o conjunto previamente conferido:

```text
python -m pip install --no-index --require-hashes --only-binary=:all: --no-deps --find-links WHEELHOUSE -r posix-py312-linux-amd64.lock
python -m scripts.backup.orchestration.posix_validation
```

O lock deve listar todas as dependências; no-deps impede resolução adicional.
[Instalações reproduzíveis pip](https://pip.pypa.io/en/stable/topics/repeatable-installs/).
Lock/wheels/venv não foram criados ou instalados nesta entrega. Integrar esse venv
ao workflow ou preparar um runner exige aprovação. Não autorizar dispatch antes
da revisão das dependências. Para isolamento de rede comprovado, bloquear egress
após a preparação, sem credenciais ou rotas operacionais. Um runner hospedado com
internet não satisfaz esse isolamento apenas por não receber Secrets/OIDC.

O harness exige **exatamente os cinco testes existentes executados, sem skips,
expectedFailures ou unexpectedSuccesses**:

- PR2: truncamento real durante leitura; FIFO/socket sem leitura bloqueante.
- PR3: ambiente do subprocesso sem credenciais herdadas, stdin/stdout streaming
  e timeout real do subprocesso POSIX.

Saída só contém contagens/códigos, nunca traceback/conteúdo de fixtures. A referência
exata de cada teste é conferida localmente; nenhum skip Windows foi removido.
Windows testa o harness com mocks e executa os guards shell usando o Git Bash já
disponível, inclusive inputs maliciosos e capitalização. Isso não valida POSIX Linux.
Depois dos cinco, executar todas as suítes PR1/2/3/controlador no mesmo Linux,
classificando skip exclusivamente Windows separadamente e exigindo symlinks,
hardlinks/dir_fd/O_NOFOLLOW efetivamente exercitados. Nada disso foi executado aqui.

Um segundo ensaio, com autorização própria, exige PostgreSQL/Nextcloud/Redis reais
em Docker, dados fictícios, sem portas públicas/credenciais AWS e rede isolada;
dependência S3 deve ser simulada localmente, sem chamar buckets existentes. Medir
drenagem, writers externos/cron, ENOSPC, dump/catalog, SIGTERM/SIGKILL/processos
órfãos, reboot/systemd, journal/fsync e retomada. Testes POSIX de arquivos não
comprovam consistência PostgreSQL/Nextcloud nem substituem restauração funcional.

## Bloqueadores e aceite

- Decidir conectividade, host key e colocação/custódia de sessão/criptografia.
- Homologar Linux, containers reais/fencing/interrupção; chave definitiva e recuperação.
- Implementar adapters operacionais auditados de captura, inventário/transferência,
  identidade/preflight e supervisor, sem converter simulador em produção por flag.
- Revisar exclusão entre deploy e captura órfã antes de ativar qualquer caminho.
- Autorizar opt-in/apply administrativo PR4 separadamente, conferir policy efetiva,
  Environment/branch/claims, sem ampliar Nextcloud/deploy ou role backup para SG.
- Autorizar código de ativação e depois dispatch operacional explicitamente.

Nenhum custo AWS foi gerado por estes testes. Futuro runner com saída fixa,
capacidade/disco local, S3 versões/requests e multipart órfãos têm custo a medir.
Backup v1 depende do bucket de dados e suas versões; não protege contra perda
completa do bucket. Integridade/AEAD não comprovam autoria ou restore funcional.
Sigsum age não verificado. Nenhuma conclusão concede autorização de Destroy.

## Validação local

Com dependências existentes dos testes PR2/controlador:

```text
python -m unittest discover -s scripts/backup/orchestration/tests -p "test_*.py" -v
python -m scripts.backup.orchestration
python -m scripts.backup.orchestration.posix_validation
```

Os dois comandos CLI são barreiras/harness; Windows recusa validação POSIX.
Não executar workflows neste PR. Código está somente pronto para revisão,
não pronto para uso operacional.

Resultados Windows após auditoria: **123 testes PR6 aprovados, sem skips**
(111 na implementação inicial). Regressões novas cobrem guards antes do checkout,
ref imutável, capitalização, inputs maliciosos, dependências exatas, rejeição de
expectedFailure, importação sem I/O operacional e códigos públicos sanitizados.
Preflight e plataforma Linux usam observações/mocks; nenhum teste POSIX foi
considerado executado por essa suíte. Regressões: PR5 144; PR1 73; PR2 187
aprovados/2 skips; PR3 71 aprovados/3 skips; IAM 21; controlador 13; storage guard
17; instalação inicial 18. IAM usa o JSON Terraform mock já disponível, sem novo
plan ou conexão AWS. PR5 usa age portátil oficial já aprovado, somente dados e
identidades descartáveis; Sigsum continua não verificado.

Actionlint dos dois workflows, YAML/HCL, AST Python, links locais, scan Gitleaks
redigido e diff-check de arquivos tracked/untracked aprovados. WSL não listou
distribuições; Docker/Ansible não foram encontrados. Nada foi instalado. Cinco
skips permanecem bloqueadores, junto com todos os ensaios de containers reais.
Os 12 arquivos alterados incluem dez novos, sem staging/commit/push, mudança em
deploy, contratos, Terraform, IAM, Compose ou Ansible. As ferramentas/relatórios
de validação permanecem em diretório ignorado.
