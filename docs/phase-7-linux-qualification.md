# Fase 7 — preparação da homologação Linux anterior ao PR7

Estado em 2026-10-10: **plano e dependências preparadas, nenhuma homologação
Linux executada**. Base local/tracking origin/main: PR6 ee7cb9d, checkout inicialmente
limpo; o remoto não foi consultado nesta etapa. Sem workflows, instalações,
AWS/EC2/S3, manutenção operacional, backup, restore, commit ou push.
Não é autorização para PR7. Normas [PR1](phase-7.md) permanecem inalteradas.

## Verificações e decisões

Foram lidos workflow/harness POSIX, dependências PR2, captura/backend/journal PR3,
hooks/inventário/criptografia/transporte/conclusion PR5, Compose, contrato PR1,
role Ansible e template systemd de captura e documentação PR6.

Fatos: harness exige Ubuntu24.04/Python>=3.11, duas distribuições nas versões
exatas e cinco testes executados sem skips/expectedFailures/unexpectedSuccesses.
Backup permanece incondicionalmente bloqueado. O workflow POSIX é manual,
contents:read, sem Environment/OIDC/Secrets e sem artifacts. Nenhuma instalação
foi executada nesta preparação; o patch prevê instalação futura só no venv privado.
O patch local passa a usar /usr/bin/python3.12, preparação por script revisto e
Python explícito do venv. **Ainda não foi publicado nem autorizado para dispatch**.
Ubuntu24.04 não garante pip/venv ou pacotes específicos: ausência falha fechada.

Recomendação: POSIX em Ubuntu24.04 x86_64 descartável com CPython3.12 e venv
privado; depois ensaios em VM Ubuntu completa com Docker e systemd. Fazer os dois
estágios separadamente. Docker sozinho não substitui reboot real nem systemd do
host; WSL não será presumido equivalente a uma VM completa. Nenhum ambiente foi
instalado/criado nesta etapa.

## Etapa A — dependências POSIX reproduzíveis

Lock e procedência preparados e revisados, sem download de wheels:

- [Lock CPython3.12/LinuxAMD64](../scripts/backup/orchestration/linux/requirements-posix-py312-linux-amd64.lock).
- [Procedência e fechamento das dependências](../scripts/backup/orchestration/linux/dependencies.provenance.json).

jsonschema==4.23.0 e rfc3339-validator==0.1.4 são os pins existentes PR2/PR6.
O lock acrescenta pins transitivos: attrs==26.1.0,
jsonschema-specifications==2025.9.1, referencing==0.37.0, rpds-py==2026.9.1,
six==1.17.0 e typing-extensions==4.15.0. Não altera requirements originais.
typing-extensions é necessária por referencing quando Python<3.13.

Para cada release foi consultado apenas JSON público oficial do PyPI por HTTPS:
nome/versão/Requires-Python, dependências e markers para Linux/CPython3.12 sem
extras, wheel não yanked compatível, filename/URL/tamanho e SHA-256 publicado.
O rpds inclui código nativo: somente wheel manylinux x86_64/ABI compatível, nunca
sdist/compilação. Um hash autorizado por pacote limita os bytes aceitos. Outra
arquitetura/Python exige lock próprio revisto, sem resolver fallback silencioso.

Isso comprova fechamento **declarado pelos metadados** e hashes publicados, não
bytes baixados, resolução pip/Linux executada, assinatura do autor ou ausência de
vulnerabilidades. HTTPS/PyPI é a fonte de confiança utilizada; não existe atestação
independente do publisher nesta preparação. Não baixar pacotes nesta etapa.
Não usar um hash calculado de download desconhecido como autenticação de origem.

### Procedimento exato futuro no executor aprovado

O executor escolhido é o runner padrão GitHub Ubuntu24.04 x64. Após revisão,
autorizações separadas para publicação e execução e conferência do SHA publicado
em main, executar manualmente apenas `backup-posix-validation.yml`, com
`confirmation=VALIDATE_POSIX`. Exemplo para essa futura autorização, **não executado**:

```bash
gh workflow run backup-posix-validation.yml --ref main -f confirmation=VALIDATE_POSIX
```

O checkout usa o SHA do evento e não persiste credenciais. O script
[prepare_posix.py](../scripts/backup/orchestration/linux/prepare_posix.py) exige
`/usr/bin/python3.12`, CPython3.12/LinuxAMD64/Ubuntu24.04 e ausência de credenciais
operacionais. O pip já disponível e seu parser vendored são pré-requisitos;
ausência ou incompatibilidade provoca falha, sem instalação/fallback. Python patch
e versão desse pip são registrados: o lock fixa pacotes, não a imagem completa
do runner ou todos os componentes do sistema.

O script baixa as oito URLs oficiais fixadas, sem proxies herdados ou redirects,
para diretório novo 0700 em RUNNER_TEMP. Confere bytes, tamanho, SHA-256 e METADATA
antes de criar o venv; qualquer diferença interrompe a preparação. Instala com
`--no-index --require-hashes --only-binary=:all: --no-deps --no-cache-dir`, usando
lock privado derivado dos registros validados, executa `pip check` e confere todas
as distribuições dentro do venv. Não reutiliza diretório/wheelhouse de tentativa
anterior. A instalação offline não elimina a rede usada no download anterior.

O harness é executado exclusivamente por
`$RUNNER_TEMP/nextcloud-posix-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT/venv/bin/python`.
Exigir os dois resultados `POSIX_VENV_PREPARED` e `FIVE_POSIX_TESTS_PASSED` e os
critérios abaixo. Timeout de job: 10min; preparação incompleta/timeout não gera
aprovação POSIX. O runner hospedado tem internet; não é air-gapped e o workflow
não comprova bloqueio de egress. Não utiliza AWS, Secrets, OIDC ou artifacts.

### Procedimento complementar para VM isolada futura

1. Aprovar plataforma, snapshot Ubuntu24.04, CPython3.12, pip/venv disponíveis e
   commit fonte exato. Verificar python3.12, python3.12-venv/ensurepip e pip; não
   presumir disponibilidade nem executar apt/get-pip se faltarem. Registrar patch
   Python, versão pip, kernel, filesystem e imagem do runner/VM. Ferramentas de
   preparação também devem ter origem e versões registradas.
2. Exportar **somente arquivos Git do commit aprovado**, nunca copiar o checkout
   com .validation-tools, .aws, chaves, state, tfvars ou configurações ignoradas.
   Não compartilhar HOME ou Docker socket com o computador operacional.
3. Em preparação separada com internet e sem credenciais, criar wheelhouse privado
   e baixar só as oito wheels fixadas. Conferir filenames/tamanhos/hashes contra
   provenance; rejeitar extras, links e arquivos especiais. Conferir também METADATA
   de cada wheel contra nome/versão/Requires-Python/Requires-Dist declarados; qualquer
   divergência exige revisão antes da instalação. Não importar pacotes antes da
   conferência. Registrar hash do lock e do conjunto de wheels.

```bash
# Somente após autorização; executar em Ubuntu24.04/CPython3.12.
# LOCK/WHEELHOUSE/VENV são caminhos privados fora do checkout, escolhidos no ensaio.
PIP_CONFIG_FILE=/dev/null python3.12 -m pip --isolated download \
  --index-url https://pypi.org/simple --require-hashes --only-binary=:all: \
  --no-deps --dest "$WHEELHOUSE" -r "$LOCK"
python3.12 -m venv "$VENV"
PIP_CONFIG_FILE=/dev/null "$VENV/bin/python" -m pip --isolated install \
  --no-index --find-links "$WHEELHOUSE" --require-hashes --only-binary=:all: \
  --no-deps -r "$LOCK"
"$VENV/bin/python" -m pip check
```

O lock completo permite no-deps; pip check deve confirmar dependências instaladas.
Não instalar outras bibliotecas implicitamente. Extras de jsonschema não foram
selecionados: rfc3339-validator é dependência direta para date-time dos contratos.
[Instalação segura pip](https://pip.pypa.io/en/stable/topics/secure-installs/),
[download e hashes](https://pip.pypa.io/en/stable/cli/pip_download/).

4. Bloquear egress e rotas a recursos operacionais **antes dos testes**. Sem NAT
   de acesso a EC2, IMDS, S3 ou serviços externos; permitir IPC/socket Unix local
   necessário aos testes. Firewall/isolamento são controles do ambiente descartável,
   não modificações à AWS. Documentar sua verificação, não inferir isolamento pela
   ausência de Secrets. Não registrar variáveis completas, HOME ou credenciais.
5. Rodar harness com o **mesmo Python do venv**; nunca system python por engano:

```bash
"$VENV/bin/python" -m scripts.backup.orchestration.posix_validation
```

Aceite: exit0/FIVE_POSIX_TESTS_PASSED, tests_run=5, failures=errors=skips=
expected_failures=unexpected_successes=0; operationally_homologated=false e
backup_complete=false. Validar também que dependência ausente/versão errada e
credenciais sintéticas provocam bloqueio sanitizado, sem consultar recursos.

Testes selecionados no harness, sem renomear/substituir por mocks:

| PR | Teste | Evidência Linux requerida |
| --- | --- | --- |
| PR2 | test_actual_concurrent_artifact_truncation | Truncamento concorrente real rejeitado pelo motivo correto |
| PR2 | test_real_fifo_and_socket_rejected_without_read | FIFO/socket reais rejeitados sem leitura bloqueante |
| PR3 | test_actual_child_environment_has_no_inherited_credentials | Subprocesso real recebe ambiente permitido, sem credenciais herdadas |
| PR3 | test_actual_child_input_and_output | stdin/stdout em processo POSIX real |
| PR3 | test_actual_child_timeout | Timeout efetivo do subprocesso local |

6. Depois executar suítes completas PR1, PR2, PR3, PR5 e PR6/controlador no mesmo
   Linux; não considerar cinco testes prova da cobertura completa. Precarregar,
   mediante autorização, ferramentas adicionais realmente requeridas: age v1.3.2
   oficial LinuxAMD64, PHP/fontes da imagem exata e PyYAML/python-hcl2 para PR6.
   **O lock atual é apenas POSIX, não resolve todas essas suítes.** Preparar locks/
   procedência separados antes de instalá-las; sem baixar age ou dependências agora.
   Sigsum do age permanece pendente; não transportar o executável Windows para Linux.
   Exigir ausência de skips POSIX; um skip exclusivo Windows deve ser identificado
   individualmente. Testar symlinks, hardlinks, dir_fd/O_NOFOLLOW e race checks.

Evidência permitida: commit/lock SHA, versões públicas, IDs dos testes, contagens,
códigos, durações e controles de isolamento. Nenhum arquivo capturado, dump,
parâmetro/secret, identidade privada ou ciphertext será artifact do GitHub.

### GitHub Actions versus ambiente local

| Alternativa | Recursos propostos | Isolamento e limitações | Estimativa incremental |
| --- | --- | --- | --- |
| GitHub runner padrão Ubuntu24.04 x64 | Baseline Linux; 10min para cinco testes, medir duração antes de aumentar | Sem OIDC/Secrets; tem internet e imagem variável. Precisa venv e lock; egress não está comprovadamente bloqueado pelo workflow atual | 5–10min por tentativa: US$0,03–0,06 fora da franquia, usando US$0,006/min; dentro da franquia pode não haver cobrança adicional |
| VM local descartável, sem runner GitHub | POSIX: 2vCPU, 2–4GiB RAM, 10–15GiB disco propostos | Snapshot, export só Git, disco próprio, sem compartilhamentos/rotas/credenciais; desligar rede após preparação | Sem cobrança AWS/GitHub; energia, disco e tempo do operador. Instalação de hypervisor/Ubuntu requer aprovação se não existentes |
| VM local para containers/systemd | 4vCPU, 8GiB RAM, 40GiB disco dedicados propostos | Ubuntu completo/root, Docker separado, datasets pequenos; reboot e ENOSPC somente discos descartáveis | Mesmos custos locais; recursos são orçamento de teste, não sizing de produção |
| Runner hospedado maior x64 | Exemplo 4vCPU, dimensionar disco/RAM pela oferta antes de contratar | US$0,012/min, sem franquia de minutos; disponibilidade depende do plano. Internet não elimina necessidade de isolamento | 30–60min: US$0,36–0,72 por tentativa; não recomendado apenas para cinco testes |
| Host Linux temporário de terceiro, fora da AWS operacional | Mesma VM completa, nenhum vínculo com VPC/conta operacional | Custódia, snapshot/firewall e cobrança contratual precisam de revisão | Preço-hora do fornecedor + disco/tráfego; não foi cotado nem autorizado |

Tarifas consultadas em 2026-10-10; valores são hipóteses de duração, não medições.
Plano/franquia/saldo do repositório não foram consultados. Sem caches/artifacts,
sem custo AWS por esses testes. Instalação/download e execução requerem autorizações
próprias. [Preços GitHub](https://docs.github.com/en/billing/reference/actions-runner-pricing),
[franquias](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

O patch POSIX preparado nesta revisão preserva dispatch/branch/sem OIDC. Ainda
precisa de aprovação/publicação e autorização de execução. Um runner hospedado não será descrito
como air gap; para ensaios de reboot/systemd, preferir a VM completa local.

## Etapa B — decisão arquitetural antes da fixture real

**Incompatibilidade comprovada, implementação desta parte suspensa para decisão:**
DockerBackend exige exatamente três serviços (Nextcloud/PostgreSQL/Redis), recusa
containers auxiliares nas redes/volumes e exige marker S3 e ausência de key/secret.
Isso impede simplesmente acrescentar um S3/IMDS emulator ao Compose de teste.
read-state.php exige installed=true, objectstore S3 igual ao marker e ausência de
key/secret; isso não comprova disponibilidade do provider de credenciais temporárias.
Não aceita instalação local como substituto. Inventário MemoryS3 do
PR5 é API Python em memória, não endpoint que Nextcloud consiga consumir.

Proposta preferida: **fixture ampliada exclusivamente de ensaio**, com allowlist
explícita de auxiliares S3/IMDS simulados, redes/labels/mounts privados verificados,
sem host networking/ports/volumes da aplicação real. Adapter de teste distinguindo
serviços escritores de serviços auxiliares, fencing verificável dos escritores
e credenciais IMDS fictícias, nunca AWS reais. Preservar todas as barreiras da CLI
e o default PR3. Implementação exige aprovação separada; nenhum guard foi relaxado.

Alternativa: serviço simulado no host descartável, com roteamento controlado para
IMDS/S3; evita container auxiliar, mas aumenta complexidade e não comprova fencing
automaticamente. Não usar esse contorno para aprovar a fixture atual.
Uma fixture local sem S3 serve apenas para subtestes PostgreSQL, **não** para aceitar
captura compatível com PR1/PR3. Não substituir guard ou inserir key/secret para fazê-la passar.

Versões/digests observados em docker/compose.yml, que não será modificado:

| Serviço | Imagem |
| --- | --- |
| Nextcloud | nextcloud:34.0.4-apache@sha256:72f5cc3347042dd5dcad72edc376b19d0e042194af344a549820affd04804c8f |
| PostgreSQL | postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826 |
| Redis | redis:7.4.8-bookworm@sha256:3e0669e42d4fe421c9dea0ba5fbc04d336b80b4f32a6c7d25bee3a1d089288a1 |

Precarregar imagens oficiais por digest depois de autorização; registrar digest
manifest/platform LinuxAMD64 e IDs locais. Em execução offline usar pull=never;
não usar tags flutuantes nem presumir imagem já presente. Emulador também precisa
pin/procedência e testes de semântica de versões/delete markers. O desenho final
propõe Moto5.1.17 no [documento da fixture S3/IMDS](phase-7-s3-imds-fixture-design.md),
com digest LinuxAMD64 conferido por metadados públicos; implementação e dependências
continuam aguardando aprovação, sem download de imagens.

O backend atualmente confere imagem fixada de Nextcloud e PostgreSQL, mas não
compara o digest Redis nessa rotina. O executor da fixture precisa conferir os
três digests; revisar esse gap no patch de teste aprovado, sem afirmar que o
preflight atual já cobre Redis. As versões acima foram obtidas do Compose real.

### Sequência dos ensaios propostos

| Ensaio | Preparação/falha injetada só na VM | Critério de aceite |
| --- | --- | --- |
| Instalação sintética | Instalação nova idêntica ao fluxo aprovado, S3 simulado antes do install, roles/secrets fictícios locais | installed=true, upload/download sintéticos, identidade/marker estáveis; nenhuma chamada AWS |
| Manutenção original off/on | Captura/recovery em ambos os estados; operação concorrente muda fingerprint/ownership | Restaura só transição própria; pré-existente permanece on; ambiguidade bloqueia sem maintenance off |
| Requisição em andamento | Upload demorado via cliente interno, ativar captura durante a requisição; observar WINCH, exit code, DB sessões | Drenagem concluída ou captura falha; nunca finalizar durante escrita; medir limites e não inferir quiescência de maintenance=true |
| Cron/CLI e escritores externos | Job Nextcloud/host, container externo e sessão PostgreSQL persistente; alteração S3 simulada sem alterar DB | Writer não controlado bloqueia antes da captura ou quebra attestation; igualdade de inventário não basta |
| Dump/config/globals | Dados conhecidos, owner/grants/extensões/sequências; custom dump + config/identidade/local_files/parameters | Catálogo íntegro, bindings/tempos/hashes corretos; pg_dumpall sem senhas; secrets 0600/dir0700, nunca argv/logs |
| Restore de verificação isolado | Segundo cluster/volumes vazios e namespace S3 simulado independente; reconstruir roles/ownership/collation e segredos fictícios | pg_restore completo, registros/seq/grants corretos, identidade Nextcloud preservada, login/download funcionais; não toca fonte nem cria restore-evidence operacional |
| SIGTERM/timeout | Interromper cada intent/etapa, repetir cancelamento | Journal durável, cleanup limitado; status incompleto e sem promotion/marker; retomada apenas de serviços próprios |
| SIGKILL/controlador órfão | Matar controlador depois de cada write-ahead intent, não Docker inteiro; observar exec/sessões remanescentes | Novo start exige recovery; sem reuso de partials; maintenance ambígua permanece; nenhuma inferência de cleanup por finally |
| Docker exec órfão | Matar/timeout cliente enquanto dump mantém sessão no daemon | Processo/sessão remoto é observado e fencing permanece; novo start não permitida enquanto writer/sessão desconhecida existe |
| ENOSPC | Disco/loop filesystem pequeno dedicado ao staging; dados sintéticos conhecidos | Parcial rejeitado, reserva/journal de falha funcionam ou intervenção explícita; nunca encher disco do host operacional |
| Atomicidade/journal/lock | Matar antes/depois fsync/rename, pending truncado, dois controladores, troca symlink/ID | Lock exclusivo liberado pelo OS após morte, active não terminal continua bloqueando; journal íntegro ou rejeição segura, sem edição manual para aprovar |
| Reboot/systemd | Reboot VM completa em pontos registrados; Docker daemon indisponível; ExecStopPost/timeout | Comportamento observado de killgroup/exec; não assumir Restart=no faz recovery no boot; definir supervisor/boot recovery antes de operação |
| Inventários pareados | Inserir versões/delete markers/paginação/Unicode e alterações durante janela | before/after dentro da mesma quiescência; captura/identity/consistency_id/tempos vinculados; ausência de controle de escritor sempre rejeita |
| Criptografia/transporte | age Linux oficial/public recipient, private key descartável no verificador isolado; simulador falha upload/leitura/marker | Plaintext permanece no host, somente ciphertext transferível, verificação VersionId exato/bytes; simulação não declara backup operacional nem restore real |

O template systemd existente é inativo: KillMode=control-group não garante matar
processo docker exec no daemon. ExecStopPost não é garantia de reboot recovery;
sem unidade de boot/supervisor homologada isso continua bloqueador. Não instalar
ou ativar unidades nesta etapa. Capturas PR3 permanecem local_candidate_only;
PR5/MemoryS3 continua simulation=true e backup_complete=false.

Cada caso precisa prova do motivo de rejeição, estado anterior/posterior, falha
injetada e duração; evidência sanitizada separada de dados privados. Captura bem
sucedida/dump listado/checksum não prova restore. Restore sintético não homologa
S3/IAM reais ou autoriza Destroy. Nenhum ensaio altera schemas normativos.

## Etapa C — matriz de prontidão para primeiro backup real

Todos os itens abaixo permanecem bloqueadores; fechar POSIX não fecha os demais.

| Bloqueador | Dependência/adapter ausente | Teste/evidência para aprovação |
| --- | --- | --- |
| Linux POSIX | Venv/wheels conferidos e executor autorizado | Cinco testes sem skips + suítes completas no target registrado |
| Fixture S3/IMDS e captura real | Decisão de fixture ampliada, init/dados sintéticos, backend ensaiado | Matriz B, dump restaurado em destino isolado e controles de writers demonstrados |
| Supervisão/órfãos | Supervisor local durável/boot recovery e ownership verificável | SIGKILL/reboot/exec órfão; nenhum candidato novo sem resolução da operação anterior |
| Deploy versus captura | Guard comum de journal/lease/lock/fencing antes de qualquer deploy/backup | Deploy sintético recusa journal pendente e captura viva/órfã; revisão separada necessária, deploy não alterado aqui |
| Conectividade | Runner com egress autorizado ou rede privada/SSM revisado; host key independente | Canal autenticado, revogação/timeout, origem restrita; não reutilizar role deploy nem conceder SG write à role backup |
| Preflight real | Collector de identidade/região/instância/serviços/disco/IAM/buckets | Dados reais autenticados, rechecagens TOCTOU, mensagens sanitizadas; booleanos sintéticos PR6 não bastam |
| Captura operacional | Backend operacional além do DockerBackend fixture e writer attestor real | Fonte exata, drain/fencing contínuo, globals/config/identity; ensaio não destrutivo aprovado previamente |
| Inventário/transferência AWS | APIs com paginação/VersionIds/existência/checksums/multipart/timeout e leitura por versão | Testes de contrato simulados antes de autorização AWS; IAM/bucket policies reais validadas depois, sem extensão automática |
| Criptografia no host | age Linux verificável, recipient definitivo, custódia externa de private key, sessão OIDC de transporte em memória ou alternativa aprovada | Roundtrip isolado/chave errada/corrupção; bytes plaintext não chegam ao runner; Sigsum/procedência explicitada |
| IAM/OIDC | Opt-in administrativo PR4 e revisão de claims/Environment/branch/policies | Apply separado autorizado; identidade/boundary/permissões efetivas verificadas; nada ativado pela publicação |
| Completion real | Produtor/verificador com conjunto fechado, hashes de bytes remotos/version binding | Marker por último; falhas/retries/conjuntos mistos rejeitados; não transpor flags do simulador para produção |
| Recuperação funcional | Custódia, PG/config/objetos das versões referenciadas e destino independente | Restore isolado aprovado, leitura funcional Nextcloud, evidência vinculada ao conjunto exato; sem autorização implícita de Destroy |

MemoryS3, InventoryHooks/attestor sintético, SimulatedSeal e Transport.simulation
não são adapters AWS. Age criptografa arquivos de verdade em testes locais, mas
essa propriedade isolada não homologa pipeline operacional. CLI de captura tem
adapter Docker de fixture real ainda não ensaiado; não aceita EC2 operacional.
Recovery PR3 recupera estado de serviços e abandona captura, não restaura dados.
Backup v1 ainda depende do bucket original e versões; não protege perda desse
bucket. Não criar PR7/PR8, IAM/SG, Object Lock ou política de retenção nesta etapa.

Custos futuros distintos da homologação: runner com saída fixa/VM dedicada, espaço
staging que comporte plaintext+ciphertext/reserva, tempo da janela, requests S3
de inventário/version read/write e armazenamento de versões/backups sem expiração,
multipart incompleto e tráfego conforme localização. Dependem do dataset/região/
plano/arquitetura ainda não escolhidos; medir tamanho e requests antes de orçamento.
Não justificar mudança IAM/conectividade por mera economia de minutos de runner.

## Autorizações seguintes e aceite desta preparação

1. Revisar o patch para o executor POSIX já escolhido (Actions Ubuntu24.04 x64),
   autorizar publicação e, separadamente, download/instalação/execução manual.
2. Aprovar arquitetura da fixture S3/IMDS, imagem/adapter auxiliares e dataset;
   só então implementar fixture e autorizar ensaios de containers/reboot.
3. Revisar resultado real de cada etapa antes de projetar ativação PR7.

Nenhuma opção autoriza AWS, operação atual, workflows de backup ou uso da chave
definitiva. Importar os módulos de preparação não baixa nem instala dependências;
o script só prepara o venv quando chamado explicitamente no executor autorizado.
Não altera deploy. Não declarar homologação em base a metadados PyPI,
testes Windows/Git Bash, mocks, checksums ou completion de simulador.

## Verificações locais desta preparação

Oito registros conferidos offline: lock igual a provenance, nomes/versões/hash
syntax, URLs oficiais, wheel tags CPython3.12/LinuxAMD64, Requires-Python e fechamento
das dependências declaradas/markers. Essa conferência não executa o resolver pip,
não lê bytes de wheels e não comprova instalação Linux.

Regressão Windows PR6/preparação: 146 testes aprovados (123 existentes + 23 novos),
zero skips nessa suíte, incluindo
guards Bash com a ferramenta já disponível. Actionlint dos dois workflows, links
locais, scan de secrets e diff-check tracked/untracked aprovados. Os cinco testes
POSIX e todos os ensaios reais continuam pendentes; não foram substituídos por mocks.
Os testes novos usam ZIPs sintéticos e mocks de subprocess/download: verificam
rejeições e fluxo offline, não representam instalação real ou bytes oficiais
baixados. Resultados Windows/Git Bash não são apresentados como execução Linux.

Regressões locais adicionais: PR1 73 aprovados; PR2 187 aprovados e 2 skips
POSIX (189 descobertos); PR3 71 aprovados e 3 skips POSIX (74 descobertos), com
PHP e Config.php oficial previamente disponíveis; controlador 13 aprovados.
As suítes de criptografia/transporte PR5 não foram reexecutadas, pois permaneceram
inalteradas. Nenhuma contagem elimina os cinco bloqueadores Linux.

Somente nove arquivos alterados/criados, sem staging/commit/push: os quatro do
plano original, workflow POSIX, script de preparação, teste novo, ajuste do teste
de workflow e desenho da fixture. Deploy, backup operacional, IAM, Terraform,
Ansible, Docker, captura/transporte e contratos normativos permanecem inalterados.
Ferramentas/relatórios auxiliares ficam ignorados pelo Git.
