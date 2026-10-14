> Registro sanitizado: ACCOUNT_ID, PUBLIC_IPV4, ADMIN_IPV4, RESOURCE_ID e lineages
> são placeholders; valores reais permanecem somente em registros privados.
> Estado atual e limites em [Fase 6](phase-6.md).

# Recuperação Ansible da aplicação — 2026-10-07

## Escopo e resultado

Somente a EC2 existente RESOURCE_ID, IPv4 PUBLIC_IPV4. Nenhum Terraform, IAM, workflow, destroy, backup ou DR foi executado. Infraestrutura e volumes preservados. Rerun interrompido no primeiro erro da aplicação, sem correção ou nova tentativa depois da falha.

O pull de imagens passou com changed=false. A tarefa Subir stack e aguardar todos os healthchecks falhou: container nextcloud-platform-nextcloud-1 is unhealthy. Recap da role: ok=19, changed=0, unreachable=0, failed=1; a task que falhou criou os containers, por isso changed=0 no recap não significa ausência de alterações reais.

Os logs sanitizados mostram o hook /docker-entrypoint-hooks.d/pre-installation/10-platform.sh e depois before-starting/10-platform.sh saindo com código 1. storage-guard.php registrava: Storage incompatível ou configuração S3 inválida; nenhuma migração realizada. Durante esse rerun, a causa interna ainda não estava determinada. A inspeção posterior confirmou CONFIG_LOAD_ERROR: apps.config.php usa OC::$SERVERROOT, mas o processo standalone do guard não definia a classe OC. Nenhuma proteção foi contornada.

## Mudanças no código

- Removido no_log somente de docker_compose_v2_pull: imagens públicas e configuração por caminhos de secrets, sem valores de senha no Compose/.env. O módulo retorna progresso/erro do pull; nenhuma configuração ou arquivo de secrets foi exibido.
- Adicionado ssh_common_args com ServerAliveInterval=30 e ServerAliveCountMax=6 para conexões sem output durante operações longas. Não é prova da causa histórica.
- Mantidos host key checking, pipelining e demais parâmetros. O transporte do workflow usa ubuntu, chave existente, ConnectTimeout=10 e StrictHostKeyChecking=yes. ANSIBLE_SSH_ARGS=-F substitui os ssh_args padrão, portanto a execução anterior não usava os defaults ControlMaster=auto/ControlPersist=60s. Multiplexação e retries não foram alterados. remote_tmp do controlador temporário usa diretórios separados para ubuntu/root; a configuração reutilizável não foi alterada nesse ponto.

## Controlador e limitações

Windows não tinha Ansible, daemon Docker local ou distro WSL. Foi preparado um venv temporário em /tmp/nextcloud-ansible-recovery-20261007 na EC2, instalando python3-venv e dependências do requirements-control.txt, community.docker 5.3.0 e sua dependência Galaxy. Esse diretório não contém cópia da chave privada ou arquivos de senha. Inventário operacional restrito a application_host, com connection=local e ansible_host=PUBLIC_IPV4. A execução foi acessada por SSH verificado usando a chave existente; isto não reproduz o transporte SSH do runner dentro do Ansible.

Pre-tasks slurp/set_fact com no_log reutilizaram em memória os cinco arquivos de senha já existentes. A role confirmou checksums e não rotacionou credenciais. Nenhuma senha foi gravada no checkout local, relatório ou logs. Configurações existentes e arquivos de secrets ficaram idempotentes. Não houve segundo rerun da role após o erro.

## Testes e comandos

- ansible platform -i inventory.json -m ansible.builtin.ping: pong.
- Ansible command com become: docker version, docker compose version, docker image ls --digests --no-trunc: sucesso.
- Ansible command: docker image inspect para cada referência, retornando somente RepoDigests: todos os oito digests coincidem com Compose.
- ansible-playbook --syntax-check e ansible-lint profile safety: sucesso antes da role. Um problema de indentação do playbook temporário gerado foi corrigido antes de executar tarefas.
- Rerun da role nextcloud_stack: imagens OK; falha no Compose wait devido a Nextcloud unhealthy.
- Ansible command com become: docker compose ps --all, free -h, df -h, docker stats --no-stream, docker system df: sucesso.
- Logs históricos SSH: autenticação do runner com chave correta registrada; sem mensagem que explique o UNREACHABLE. Kernel sem evento OOM correspondente no intervalo consultado. Causa histórica permanece indeterminada.
- Estado/logs Nextcloud coletados sem modificar o container. Senhas conhecidas redigidas em memória antes de qualquer output; env/config completo não exibido.

## Digests confirmados

| Imagem | Digest |
| --- | --- |
| `postgres:17.11-bookworm` | `sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826` |
| `redis:7.4.8-bookworm` | `sha256:3e0669e42d4fe421c9dea0ba5fbc04d336b80b4f32a6c7d25bee3a1d089288a1` |
| `nextcloud:34.0.4-apache` | `sha256:72f5cc3347042dd5dcad72edc376b19d0e042194af344a549820affd04804c8f` |
| `prom/prometheus:v3.15.0` | `sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e` |
| `grafana/grafana:13.2.3` | `sha256:b28bae15e219c998fb0e0424ed724930cc61b1f61fb404d47c862f9a23f9e572` |
| `grafana/loki:3.7.8` | `sha256:1107dd5274e0ada47e42472b7a7e71f3b2a2fe878878108f3e2f9e51528f0193` |
| `grafana/alloy:v1.20.1` | `sha256:2aa2099af76c0098d4af7a4d6e48f86cb66dc1a000222ad927a1c67c6542d13f` |
| `prom/node-exporter:v1.12.1` | `sha256:1b4e4438faca4dd7e001dd445d161a4a2091b0fededa84093b3a8dfeae1f1be0` |

## Estado dos containers

| Serviço | Resultado observado |
| --- | --- |
| Nextcloud | restarting, unhealthy, exit code 1, OOMKilled=false |
| PostgreSQL | healthy |
| Redis | healthy |
| Prometheus | healthy |
| Grafana | healthy |
| Node Exporter | healthy |
| Loki | running; readiness não validada |
| Alloy | running; readiness não validada |

As validações posteriores da role (integração Nextcloud/PostgreSQL/Redis/S3, datasources/dashboard e readiness internos) não foram alcançadas. HTTP Nextcloud não respondeu. URL prevista http://PUBLIC_IPV4 ainda indisponível. Túnel Grafana não foi validado após a falha; não se declara deploy concluído.

## Medições

- free -h: total 3,7 GiB; usada 1,1 GiB; disponível 2,6 GiB; swap 0.
- df -h: raiz 29 GiB, usados 9,7 GiB, livres 19 GiB, 35%.
- docker system df: oito imagens 6,428 GB; oito containers ~262 kB; seis volumes ~825 MB; build cache zero.
- docker stats --no-stream (amostra): Grafana 328,9 MiB / 0,83% CPU; PostgreSQL 40,0 MiB / 0,03%; Redis 8,9 MiB / 0,30%; Prometheus 52,5 MiB / 3,47%; Loki 113,6 MiB / 0,84%; Alloy 59,2 MiB / 0,62%; Node Exporter 13,3 MiB / 0%; Nextcloud em restart sem amostra útil.

Medições com Nextcloud indisponível não dimensionam a stack completa.

## Warnings

- --tree dos testes ad-hoc está deprecated (remoção prevista no ansible-core 2.23); não faz parte do código operacional.
- ansible-lint advertiu sobre PATH do venv e nova versão disponível; nenhuma versão foi alterada além das dependências permitidas pelo requirements existente.
- Galaxy inicialmente advertiu sobre collections_path: execuções usaram ANSIBLE_COLLECTIONS_PATH explícito.
- Debconf sem terminal durante instalação de python3-venv; instalação concluída.
- Nenhum warning de inventory no ad-hoc/rerun com inventário real.

## Diagnóstico posterior e correção local do guard

Na inspeção sanitizada, config.php, zz-platform.config.php e .platform-storage.json estavam ausentes; installed=false efetivo. A configuração esperada usa a classe \\OC\\Files\\ObjectStore\\S3, bucket nextcloud-platform-data-ACCOUNT_ID-lab e região us-east-1, sem key/secret. O erro ocorria durante a leitura de apps.config.php, antes de comparar storage ou consultar S3.

MetadataOptions já estavam corretas: HttpEndpoint=enabled, HttpTokens=required, HttpPutResponseHopLimit=2. IMDSv2 e Instance Role funcionaram no host e no container de diagnóstico; GetBucketLocation/ListBucket retornaram sucesso e GetObject de chave inexistente retornou NoSuchKey. Nenhum objeto foi criado ou apagado. Não há correção de infraestrutura associada a este patch.

A implementação foi comparada com [OC\\Config da versão 34.0.4](https://raw.githubusercontent.com/nextcloud/server/v34.0.4/lib/private/Config.php) e com os nove arquivos *.config.php e lib/private/Config.php extraídos, somente para leitura, da imagem fixada acima. Essas fontes públicas ficam em diretório local ignorado, sem config.php operacional ou secrets.

O guard fornece uma classe OC mínima, exclusiva do processo standalone, contendo SERVERROOT=/var/www/html e configDir. Os arquivos oficiais inspecionados precisam apenas desse contexto de caminhos, particularmente apps.config.php. Não carrega lib/base.php, autoloader ou serviços da aplicação; não inicializa banco, cache ou instalação. A execução de PHP de configuração continua sendo código administrativo confiável, não um sandbox para arquivos arbitrários.

A leitura inclui config.php quando presente e todos os *.config.php, com ordenação natural, merge recursivo array_replace_recursive, LOCK_SH, invalidação de opcache quando disponível e escopo isolado. Arquivos extras não são ignorados. Referências a contexto OC não suportado interrompem a execução. A precedência acompanha o leitor oficial; requisitos de segurança podem ser mais estritos: NC_installed não pode ocultar uma instalação já registrada, e um objectstore conflitante também é recusado em instalação parcial.

Diagnósticos em stderr contêm apenas reason code e nome sanitizado do arquivo, sem valores de CONFIG/env, mensagens de exceção ou stack trace:

- CONFIG_LOAD_ERROR: descoberta/leitura/avaliação de configuração falhou, inclusive contexto indisponível, warnings, output inesperado ou exit/die;
- EXPECTED_STORAGE_CONFIG_ERROR: configuração S3 esperada inválida;
- MARKER_READ_ERROR: marcador ilegível ou JSON inválido;
- MARKER_STORAGE_MISMATCH: identidade do marcador diferente da esperada;
- INSTALLED_WITHOUT_STORAGE_MARKER: instalação existente sem adoção explícita registrada;
- OBJECTSTORE_CONFIG_MISMATCH: objectstore existente incompatível com o esperado;
- MARKER_WRITE_ERROR: não foi possível registrar a identidade inicial.

Continuam bloqueadas instalação local existente, troca de bucket/região/classe/opções, credenciais estáticas inesperadas, marcador inconsistente e migração automática. O marcador válido existente não é regravado. Erros de configuração nunca significam aprovação do storage. Saída de configuração é capturada; exceptions/warnings não expõem seus textos; exit(0) dentro de configuração também não pode produzir sucesso.

## Validações locais desta correção

PHP CLI local 8.5.11, sem php.ini (-n); a validação na imagem/EC2 com o patch ainda está pendente. Nenhum deploy Ansible, workflow ou Terraform foi executado nesta etapa.

Comandos executados no checkout Windows:

```powershell
Get-ChildItem docker/*.php | ForEach-Object {
    & .validation-tools/php/php.exe -n -l $_.FullName
}
$env:STORAGE_GUARD_OFFICIAL_CONFIGS = (Resolve-Path .validation-tools/nextcloud-34.0.4-source).Path
python docker/tests/test_storage_guard.py .validation-tools/php/php.exe
python docker/tests/check_storage.py .validation-tools/php/php.exe
$env:PYTHONPATH = (Resolve-Path .validation-libs).Path
python ansible/tests/check_static.py
python docker/tests/check_static.py
git diff --check
```

Resultados: lint dos quatro arquivos PHP aprovado; 17 testes de regressão aprovados, incluindo leitura dos nove configs oficiais, instalação nova sem config.php, apps.config.php, S3 válido, recusa de incompatibilidades, ordenação natural/merge recursivo, sanitização e idempotência após instalação. O teste opcional dos arquivos oficiais requer STORAGE_GUARD_OFFICIAL_CONFIGS; os outros 16 não dependem de Docker, rede ou fixtures externas. Os arquivos temporários dos testes não contêm credenciais reais.

check_storage.py aprovado. Contratos estáticos Ansible: 15 arquivos YAML/Jinja e 59 tasks aprovados. Contratos Docker/Compose/S3 aprovados; nenhum container ou volume criado. O contrato Ansible foi atualizado apenas para refletir o diagnóstico visível do pull público com policy=missing; compose up/restart e probes que manipulam informação sensível continuam exigindo no_log=true. Ansible syntax-check/lint desta etapa e Compose CLI não foram executados no Windows; os resultados anteriores do controlador temporário são históricos e não equivalem a testar este patch em produção.

## Aplicação segura futura — requer autorização separada

1. Revisar/publicar somente os arquivos esperados, depois da autorização. Não usar deploy.yml mode=apply ou Terraform para esta correção.
2. Validar o guard corrigido na mesma imagem fixada, usando fixtures sem secrets e diretórios temporários; transferir apenas storage-guard.php para seu caminho atual no host, preservando ownership/permissões e todas as configurações/secrets existentes. Não executar o guard manualmente contra o volume real como teste de leitura: na primeira instalação ele grava o marcador.
3. Retomar somente Nextcloud pelo Compose já existente. Como a cópia atômica pode trocar o inode de um arquivo bind-mounted, recriar somente esse container (up -d --no-deps --force-recreate nextcloud) garante leitura do novo arquivo. Preservar volumes; não usar down, -v, limpeza de banco ou recriação de dependências.
4. Validar logs sanitizados do hook, occ status, healthcheck e integração S3/PostgreSQL/Redis; então conferir serviços de observabilidade e HTTP. Confirmar installed e identidade do storage, sem imprimir config.php/env/secrets. Se o entrypoint não concluir a instalação nova, parar para diagnóstico; não forçar reset ou migração.

Na revisão inicial, as alterações ainda não estavam commitadas, publicadas ou aplicadas na EC2. Estado operacional histórico permanece incompleto; o êxito dos testes não declara o Nextcloud recuperado. Nenhuma Fase 7 iniciada.

## Preparação da retomada automatizada autorizada

A autorização posterior permite publicar somente os seis arquivos revisados e retomar deploy.yml em mode=apply apenas se persistent/runtime estiverem sem mudanças. A task Copiar configuração única da stack e probes inclui storage-guard.php a partir de playbook_dir/../../docker, portanto o checkout GitHub é a fonte da correção; não haverá patch exclusivo na EC2.

Antes da publicação, o guard foi validado também na imagem oficial nextcloud:34.0.4-apache com o digest registrado acima: PHP 8.5.11, lint aprovado, os 17 testes aprovados e check_storage.py aprovado. Containers temporários usaram --network none, --read-only e somente fixtures públicas em diretório isolado, sem montar volumes/config/secrets da aplicação. Nenhuma alteração foi feita no guard operacional durante esses testes. Gitleaks não encontrou secrets nos diretórios Ansible, Docker e docs.

A retomada exige conferência dos 12 recursos persistent, 15 runtime, IPv4 administrativo/CIDRs, volumes e serviços existentes, seguida de plans 0/0/0. Qualquer divergência ou nova falha interrompe a execução, preservando infraestrutura e dados. O resultado operacional será apresentado após o workflow; ainda não há declaração de recuperação neste registro de preparação.
