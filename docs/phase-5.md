> Registro da entrega original da Fase 5. As referências a loopback/túnel abaixo
> são históricas; o acesso atual proposto na revisão da Fase 6 está em
> [acesso Grafana](grafana-browser-access.md), ainda aguardando aplicação autorizada.

# Fase 5 — Observabilidade local

Configuração implementada para futura execução na mesma EC2 da aplicação. Somente
validações locais/estáticas executadas; nenhum apply/destroy, recurso AWS, VM,
Docker daemon/container ou servidor de observabilidade iniciado. Não foram criados
GitHub Actions, OIDC operacional, backup, destroy workflow, DR ou Azure. S3/IAM/
instalação da Fase 4 e instance_type permanecem preservados. **Parar antes da Fase 6.**

## Entrega e arquitetura

Compose integra quatro componentes pedidos e Node Exporter, totalizando oito
serviços com a aplicação existente. Host métricas → Node Exporter → Prometheus;
Nextcloud status/PG+Redis TCP → Blackbox embutido Alloy → remote_write Prometheus;
JSON logs Docker → Alloy → Loki. Grafana consulta Prometheus/Loki por proxy e
provisiona dashboard de 11 painéis por arquivo, sem setup manual na interface.

Node Exporter tem uso concreto: CPU, memória, disco/filesystem, load e uptime do
host. Não há exporters PG/Redis ou cAdvisor. Probes TCP não representam consultas
autenticadas ou estado Docker; métricas por container não foram inventadas.
Blackbox embutido no Alloy evita adicionar um serviço extra. Job é normalizado
para nextcloud/postgres/redis porque a versão fixada emite um prefixo no default.

Nextcloud status exige HTTP200/installed=true/maintenance=false/needsDbUpgrade=false,
com Host renderizado do trusted domain existente, sem mudar a configuração Nextcloud.
S3/config/guard/perfil IAM da Fase 4 não receberam alterações. Aplicação recebeu
apenas logging json-file rotacionado, com labels Compose para identificar logs.

## Imagens/versões

| Serviço | Imagem fixada |
| --- | --- |
| Nextcloud (preservado) | nextcloud:34.0.4-apache |
| PostgreSQL (preservado) | postgres:17.11-bookworm |
| Redis (preservado) | redis:7.4.8-bookworm |
| Prometheus | prom/prometheus:v3.15.0 |
| Grafana | grafana/grafana:13.2.3 |
| Loki | grafana/loki:3.7.8 |
| Alloy | grafana/alloy:v1.20.1 |
| Node Exporter | prom/node-exporter:v1.12.1 |

Todas têm digest SHA-256 explícito em docker/compose.yml. Novos manifests multiarch
Linux amd64/arm64 confirmados no registry oficial; releases oficiais consultadas.
Validadores Windows da mesma versão baixados dos releases oficiais e verificados
contra SHA256SUMS/sha256sums.txt, isolados em .validation-tools ignorado. Não houve
pull/up de imagens nem instalação de serviços. Registry confirma existência, não
funcionamento. Não usar latest nem atualizar versões/digests sem revisão/testes.

## Segurança, rede e credenciais

Grafana publica somente 127.0.0.1:3000 (porta parametrizada); acesso preferido por
túnel SSH. Prometheus/Loki/Alloy/exporter sem portas publicadas. observability é
rede internal; Alloy também backend para probes. Nenhum serviço de observação entra
na frontend, recebe credentials AWS ou ganha política S3. Terraform/SG/IAM não foram
alterados. Bloqueio real de egress/IMDS continua a validar na VM; não alegar isolamento
entre processos privilegiados de um host com instance profile compartilhado.

GRAFANA_ADMIN_USER/GRAFANA_ADMIN_PASSWORD vêm do controlador/Vault; sem senha fixa.
Password com 32–256 caracteres alnum/_/- vira quinto secret no host root:root,
diretório 0700/arquivo 0444, montado apenas no Grafana. Traversal host protege o
arquivo, mas root/operador Docker pode lê-lo. .env contém somente referência e nomes.
Checksums impedem rotação acidental/no_log protege tarefas sensíveis. Senha/admin env
só inicializam Grafana; não rotacionam SQLite existente. Manter valores estáveis.

Mounts e justificativas:

| Mount | Uso | Risco/mitigação |
| --- | --- | --- |
| Node /proc:ro | CPU/memória/stat/mountinfo host | Metadata host; UID nobody/cap_drop ALL |
| Node /sys:ro | Kernel/dispositivos/disco | Leitura readonly, sem host PID/network |
| Node /:ro,rslave | statfs dos filesystems reais | Leitura ampla conforme UID; rslave recebe mounts sem propagação ao host |
| Alloy data-root/containers:ro | JSON logs/attrs Docker | UID0 para arquivos root-owned; lê também metadata/outros logs nesse diretório |

Alloy UID0/GID473 escreve seu volume original group473, cap_drop ALL/no-new-privileges/
rootfs readonly. Node Exporter nobody com mesmas restrições. Nenhum privileged,
socket Docker ou daemon TCP exposto. Socket ro não impede chamadas API de mutação;
JSON readonly evita o plano de controle, mas não é isolamento por cliente. Logs
podem conter dados sensíveis; outros projetos são descartados após ler/parsing,
antes do envio Loki. Filename removido reduz cardinalidade. Arquivos internos do
daemon nunca são escritos/rotacionados pelo coletor; validar após upgrades/rotação.

Docker rootful/Linux com json-file/labels por serviço é pré-requisito. Data-root
alternativo: nextcloud_stack_docker_log_directory, existente/root-owned e terminando
em containers; bind create_host_path=false evita coletar pasta vazia acidentalmente.

## Retenção e capacidade

- Prometheus scrape/evaluation 30s, retenção 48h e blocos até 1GB; head/WAL/transientes
  não entram no limite de blocos. Preservar somente o histórico necessário ao lab.
- Loki single node/TSDB v13/filesystem, 24h de retenção, compactor a cada 10m/delete
  delay 1h/2 workers; ingestão ~32KiB/s e burst128KiB, até 256 streams. Caches32/16MB,
  replay WAL128MB, baixa concorrência de queries. Não são limites da RAM total.
- Docker logs 5m × 3 arquivos × 8 serviços (~120MiB nominais), mais transientes/
  containers antigos. Coleta decodifica log/stream/time; labels project/service via attrs.
- Alloy posições persistem; sem posição começa no fim (logs iniciais podem ser
  omitidos), metrics WAL mantém 5–30m/truncate10m. Sem WAL extra dos logs e retries
  limitados: backpressure/429/indisponibilidade podem perder logs.

Retenção temporal/ingestão não são quota total de disco. 32KiB/s por 24h representam
~2.6GiB brutos antes de compressão e overhead; compactor exclui de forma assíncrona.
Somar índices/WAL/head, imagens, PostgreSQL e dados auxiliares Nextcloud. Alertar
disco livre não impede enchimento. **4GiB da t3.medium e EBS12GiB precisam de medição**;
nenhuma suficiência foi afirmada, tamanho alterado ou hard limit de CPU/RAM adicionado.

Volumes prometheus_data/grafana_data/loki_data/alloy_data são descartáveis e ficam
fora de backup/DR futuro. Histórico pode ser perdido após destroy; Git/Ansible
reconstituem configs/dashboards, não histórico/estado local Grafana. Não usar S3 logs.

## Alertas e Ansible

Quatro regras reais no Prometheus: NextcloudUnavailable por 2m (zero ou ausência),
RootDiskLow (<10% livre por5m), HostMemoryPressure (<10% disponível por5m),
AlloyUnavailable (up=0 por2m). Dashboard ALERTS mostra pending/firing. Engine Grafana
sem regras próprias desabilitado; não há Alertmanager/e-mail/Slack/serviço externo.

Role nextcloud_stack prepara arquivos e secret, instala python3-requests para
inspeção Docker, faz pull missing/up recreate auto/wait. Configs não secretas
0755/0644; blackbox Host renderizado de variável já existente. Copy/template changed
seleciona restart só de observabilidade afetada, dependencies=false. Primeiro deploy
pode reiniciar após up; rerun idêntico não reinicia. Bind paths iguais não fazem
Compose detectar conteúdo alterado automaticamente. Node Exporter muda via Compose.

Prometheus/Grafana/Node Exporter têm healthchecks HTTP com ferramentas das imagens.
Loki distroless e Alloy sem cliente HTTP são verificados por uri via bridge após
docker_container_info local (nomes single replica project-service-1). Não usar CLI
verify-config como healthcheck falso. Ansible também espera Grafana SQLite healthy,
datasources healthy e dashboard UID. Bootstrap.yml permanece só common/docker.
Readiness/datasources não provam ingestão de logs, probes ou render; passos em VM abaixo.

## Árvore e arquivos criados/alterados

```text
nextcloud-platform/
├── .gitattributes                                      # alterado
├── AGENTS.md                                           # alterado
├── README.md                                           # alterado
├── docker/
│   ├── compose.yml                                     # alterado
│   ├── .env.example                                    # alterado
│   ├── README.md                                       # alterado
│   └── tests/check_static.py                           # alterado
├── ansible/
│   ├── README.md                                       # alterado
│   ├── tests/check_static.py                           # alterado
│   └── roles/nextcloud_stack/
│       ├── defaults/main.yml                           # alterado
│       ├── templates/env.j2                            # alterado
│       └── tasks/
│           ├── main.yml                                # alterado
│           ├── monitoring.yml                          # novo
│           └── validate-monitoring.yml                 # novo
├── monitoring/
│   ├── README.md                                       # alterado
│   ├── prometheus/
│   │   ├── prometheus.yml                              # novo
│   │   └── rules/alerts.yml                            # novo
│   ├── grafana/
│   │   ├── provisioning/datasources/datasources.yml     # novo
│   │   ├── provisioning/dashboards/provider.yml         # novo
│   │   └── dashboards/platform-overview.json            # novo
│   ├── loki/loki.yml                                   # novo
│   ├── alloy/
│   │   ├── config.alloy                                # novo
│   │   └── blackbox.yml.j2                              # novo
│   └── tests/
│       ├── alerts.test.yml                             # novo
│       └── check_static.py                             # novo
└── docs/
    ├── decisions.md                                    # alterado
    └── phase-5.md                                      # novo
```

Nenhum arquivo Terraform/S3/guard PHP/instalação Nextcloud foi alterado. Helpers,
receipts e binários locais de validação ficam apenas em .validation-tools ignorado;
não são componentes da stack ou workflows. blackbox.yml real é renderizado no host,
não versionado com domínio/IP real no repositório.

## Comandos executados e evidência

Raiz E:\NextCloud\nextcloud-platform, PowerShell/Windows. Consultas HTTP somente
fontes oficiais de releases/registry/documentação; nenhum request à conta AWS.

```powershell
# Consultas releases oficiais + manifests Docker; download/checksum local:
. .validation-tools/prepare-monitoring.ps1
# Geração de arquivo JSON de dashboard, sem Grafana servidor:
python .validation-tools/generate-dashboard.py

$env:PYTHONPATH = Join-Path (Get-Location) '.validation-libs'
python ansible/tests/check_static.py
python docker/tests/check_static.py --docker-cli .validation-tools/docker.exe --docker-config .validation-tools/config
python docker/tests/check_storage.py .validation-tools/php/php.exe
python monitoring/tests/check_static.py --promtool .validation-tools/prometheus/prometheus-3.15.0.windows-amd64/promtool.exe --loki .validation-tools/loki/loki-windows-amd64.exe --alloy .validation-tools/alloy/alloy-windows-amd64.exe

# Validators nativos (também usados pelo teste monitoring):
& .validation-tools/prometheus/prometheus-3.15.0.windows-amd64/promtool.exe check config --syntax-only monitoring/prometheus/prometheus.yml
& .validation-tools/prometheus/prometheus-3.15.0.windows-amd64/promtool.exe check rules monitoring/prometheus/rules/alerts.yml
# O script executa: promtool test rules monitoring/tests/alerts.test.yml
# e promtool --experimental promql format <cada consulta do dashboard>.
# --experimental habilita só esse comando de parser; não configura feature runtime.
& .validation-tools/loki/loki-windows-amd64.exe '-config.file=monitoring/loki/loki.yml' '-verify-config=true'
& .validation-tools/alloy/alloy-windows-amd64.exe fmt -w monitoring/alloy/config.alloy
& .validation-tools/alloy/alloy-windows-amd64.exe validate monitoring/alloy/config.alloy
# O script monitoring também faz Alloy fmt --test e validate com blackbox fixture temporário.

# PHP anterior: para cada um dos quatro arquivos docker/*.php:
.validation-tools/php/php.exe -n -l <arquivo.php>
Get-Command ansible-playbook,ansible-lint -ErrorAction SilentlyContinue

terraform fmt -recursive
terraform fmt -check -recursive
# Em bootstrap, persistent e runtime:
terraform -chdir=terraform/aws/<root> validate
terraform -chdir=terraform/aws/<root> test
```

Resultados:

- Compose config --quiet real aprovado com fixtures; rejeita admin/secret Grafana,
  path logs e entradas anteriores obrigatórias vazias. Nenhum daemon necessário.
- YAML/JSON/Jinja/contracts aprovados; Ansible 15 arquivos/59 tasks. Seleção de
  restart testada: rerun vazio, serviços afetados apenas e deduplicação.
- Promtool config syntax/rules, quatro cenários de alerta e parser das dez consultas
  PromQL aprovados. Inclui pending, outage, saudável, no-data e recuperação.
- Loki -verify-config e Alloy validate/fmt aprovados nos binários nativos oficiais;
  Alloy valida sintaxe/componentes, não executa descoberta/probes. Blackbox template
  e regex testados com estados installed/maintenance/upgrade/erro e Host correto.
- Contratos S3 da Fase 4, guard PHP fresh/rerun/recusa de troca/local, PHP lint e
  Terraform fmt/validate/test preservados. Terraform: 4+2+2=8 testes mock/plan,
  sem apply/destroy real ou backend alterado.
- Ansible syntax-check/lint não executados: executáveis indisponíveis neste Windows.
  YAML/Jinja não os substituem. Sem render Grafana, coleta ou medição real.

## Teste operacional futuro — não executado

Somente com VM autorizada, controlador Linux, SSH/host key verificados, S3/IAM
da Fase 4 operacionais e credenciais externas injetadas. Nenhum passo abaixo foi
executado nesta entrega.

1. Instalar requirements/collection Ansible; executar syntax-check e ansible-lint.
   Rodar deploy.yml e guardar recap. Confirmar oito serviços running/healthchecks,
   readiness Loki/Alloy, datasources e UID platform-overview. Não down/prune volumes.
2. Confirmar login/upload/download Nextcloud e probe S3 da Fase 4; monitoramento
   não substitui funcionalidade de armazenamento. Verificar probe_success{job="nextcloud"}=1.
3. Via Grafana datasource Prometheus, consultar up, node_cpu_seconds_total{job="host"},
   node_memory_MemAvailable_bytes, node_filesystem_avail_bytes{mountpoint="/"} e
   node_boot_time_seconds. Comparar com free/df/uptime da EC2, não namespace Docker.
   PG/Redis probe_success=1 significa somente TCP.
4. Gerar atividade Nextcloud após Alloy estar ready. Em Explore Loki consultar
   {job="docker",project="<project Compose>",service="nextcloud"}; confirmar log/stream,
   timestamps e ausência de outros projetos/filename como label. Testar rotação e
   restart preservando cursor; não esperar histórico anterior ao primeiro tail.
5. Confirmar health dos dois datasources, dashboard11painéis, CPU/mem/disco/uptime,
   disponibilidade, logs e ALERTS. Faltas de dados devem ser investigadas, não mascaradas.
6. Em janela de demo sem usuários reais, a partir do diretório Docker **da VM**:
   `sudo docker compose stop nextcloud`. Esperar mais de 2m + ciclos30s; confirmar
   probe_success=0 e NextcloudUnavailable firing no painel (também via API /api/v1/rules).
   Grafana/Prometheus devem permanecer disponíveis. Não remover volumes ou objetos S3.
7. `sudo docker compose up -d --wait nextcloud`; confirmar status/login/upload,
   probe_success=1 e desaparecimento do alerta. Não transformar esse teste em backup/DR.
8. Medir `docker stats --no-stream`, `free -h`, `df -h /`, `docker system df` e
   volumes/WAL/índices com leitura administrativa. Repetir com upload e carga de logs;
   observar rate-limit/drops e compactor após 24h+delay. Registrar CPU/RAM/disco/picos,
   créditos T3 e cardinalidade antes de ajustar sizing/retention; não presumir 4GiB suficientes.
9. Rodar Ansible novamente com entradas idênticas: conferir ausência de restarts
   e alterações persistentes (APT cache pode atualizar conforme validade). Modificar
   configuração de teste autorizada e confirmar restart só do serviço afetado,
   depois um rerun estável. Coleta/volumes/app devem permanecer funcionais.
10. Confirmar com inspeção Docker/host que só Nextcloud e Grafana loopback têm
    ports, nenhum socket/privileged e que observabilidade não acessa IMDS/S3/Internet.
    Não testar S3 via writes de serviços de observabilidade. Manter SG e tunnel.

Túnel Grafana preferido para porta default:

```bash
ssh -N -L 127.0.0.1:3000:127.0.0.1:3000 "${PLATFORM_SSH_USER}@${PLATFORM_HOST}"
```

## Riscos e pendências

Sem VM, ficam pendentes capacidade 4GiB/12GiB, mounts/proc reais, JSON attrs/rotação,
job/remote_write/coleta, bootstrap Grafana/secret/provisioning/render, healthcheck
das imagens e duas execuções idempotentes. Retenção não é quota/durabilidade; Loki
rate-limit/retries/tail podem perder logs e volumes são descartáveis. Mounts ro
permitem leitura de dados sensíveis; não são isolamento por cliente ou proteção
contra host privilegiado. Rede interna exige verificação real de egress/IMDS.

**Fase 5 concluída no código, testes operacionais pendentes. Aguardar nova instrução.**

Fontes: [Monitoring e decisões](../monitoring/README.md),
[Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/),
[Loki retention](https://grafana.com/docs/loki/latest/operations/storage/retention/),
[Alloy Blackbox](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.blackbox/),
[Alloy targets v1.20.1](https://github.com/grafana/alloy/blob/v1.20.1/internal/component/prometheus/exporter/blackbox/blackbox.go),
[Docker JSON logs](https://docs.docker.com/engine/logging/drivers/json-file/),
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/).
