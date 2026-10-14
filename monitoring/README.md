> Estado atual: deploy automatizado validado na AWS, conforme a [Fase 6](../docs/phase-6.md).
> Os registros de testes/limitações das entregas originais abaixo são históricos;
> consultar o relatório atual para distinguir validações concluídas de pendências.

# Observabilidade local — Fase 5

Prometheus/Grafana/Loki/Alloy e Node Exporter na mesma EC2. Nenhuma VM/container
executado nesta entrega. Sem backend AWS gerenciado, S3 para logs ou IAM novo.

```text
host /proc + /sys + statfs ── Node Exporter ── scrape ── Prometheus ── Grafana
status.php / TCP PG e Redis ── Blackbox no Alloy ── remote_write ──┘
logs JSON Docker ── Alloy (arquivos read-only) ── Loki ── Grafana
```

## Imagens e coleta

| Serviço | Tag fixada | Função |
| --- | --- | --- |
| Prometheus | prom/prometheus:v3.15.0 | Host, self, Alloy/Loki/Grafana; recebe probes Alloy |
| Grafana | grafana/grafana:13.2.3 | Datasources/dashboard provisionados |
| Loki | grafana/loki:3.7.8 | Monolítico filesystem/TSDB v13, uma réplica |
| Alloy | grafana/alloy:v1.20.1 | Logs JSON e Blackbox integrado |
| Node Exporter | prom/node-exporter:v1.12.1 | CPU/memória/filesystem/disco/load/boot/time/uname |

Digests multiarch fixados em docker/compose.yml; Linux amd64/arm64 confirmados no
registry oficial, sem comprovar execução. Imagens Nextcloud/PostgreSQL/Redis preservadas.
Atualizações exigem revisão/testes, sem latest ou atualização automática.

Scrape/evaluation a cada 30s. Nextcloud: HTTP status.php na rede backend, Host do
primeiro trusted domain existente; exige HTTP 200, installed=true, maintenance=false
e needsDbUpgrade=false. Não altera Nextcloud, S3 ou trusted domains. discovery.relabel
normaliza job para nextcloud/postgres/redis e instance para o endpoint. Probes chegam
por remote_write interno, não aparecem como targets diretos Prometheus: consultar
probe_success e componentes Alloy. Blackbox embutido evita um serviço adicional.

PG/Redis: somente TCP, sem senhas. Prova porta alcançável, não SQL/PING autenticado
ou integridade. Probe da Fase 3 continua validando consulta/autenticação no deploy.
Sem exporters dedicados de banco/cache. cAdvisor avaliado e omitido: suas métricas
por container não são requisito mínimo e mounts/coleta aumentariam exposição e
consumo. Dashboard mostra disponibilidade dos serviços, sem inventar estado Docker
ou CPU/RAM por container. Essas métricas individuais permanecem fora desta fase.

## Grafana e alertas

Datasources platform-prometheus/platform-loki são proxy/server-side, não editáveis.
Dashboard platform-overview tem 11 painéis: CPU, memória, disco usado/livre, uptime,
Nextcloud, TCP PG/Redis, scrapes, latência, alertas e logs por projeto/serviço.
PromQL validada; carga/renderização real do dashboard ainda exige VM.

GRAFANA_ADMIN_USER/GRAFANA_ADMIN_PASSWORD chegam do controlador/Vault, sem defaults
de credenciais. Password vira secret protegido e GF_SECURITY_ADMIN_PASSWORD__FILE;
repo/.env não contém senha. Reruns exigem o mesmo secret; env não rotaciona conta/
senha em SQLite existente. Signup/anônimo, analytics e preinstall/update de plugins
desabilitados. Sem plugins externos necessários.

Regras: NextcloudUnavailable (probe=0/ausente por 2m), RootDiskLow (raiz <10% livre
por 5m), HostMemoryPressure (<10% disponível por 5m), AlloyUnavailable (scrape down
por 2m). ALERTS mostra pending/firing no dashboard. Sem Alertmanager/notificação
externa. Engine Grafana desabilitado: regras vivem no Prometheus. Testes promtool
cobrem indisponível/pending, saudável, ausência de telemetria e recuperação.

## Retenção, armazenamento e consumo

| Dado | Configuração | Limite real |
| --- | --- | --- |
| Prometheus | 48h ou retention.size=1GB | Blocos; head/WAL/transientes ficam além do limite |
| Loki | 24h; compactor 10m, delay 1h, 2 workers | Exclusão assíncrona, sem quota de bytes |
| Loki ingestão | 0.03125 MiB/s (~32 KiB/s), burst 0.125 MiB, até 256 streams | Pode rejeitar bursts/causar perda; não garante espaço |
| Loki cache/replay | Chunks 32MB, resultados 16MB, WAL replay 128MB | Não limita RAM total |
| Docker JSON | 5m × 3 arquivos × 8 serviços (~120 MiB nominais) | Somar containers antigos/transientes e overhead |
| Alloy | Cursors locais; metrics WAL 5–30m, truncate 10m | Limite por tempo, sem quota de bytes |

Tail inicia no fim sem posição prévia; alloy_data guarda cursors. Retries limitados
podem perder logs sob pressão. JSON Docker é decodificado; labels project/service
vêm de attrs Compose, stream de stdout/stderr. Filename removido reduz cardinalidade;
outros projetos são descartados antes de enviar. Sem API/socket de descoberta.

Volumes prometheus_data/grafana_data/loki_data/alloy_data são descartáveis, fora do
futuro backup/DR. Git/Ansible repõem configs/dashboards após destroy; histórico e estado
Grafana podem ser perdidos. Retenção local não muda proteção S3 da Fase 4.

RAM/CPU não medidos. t3.medium (4 GiB) continua default a validar, sem afirmar suficiência;
EBS default atualizado para 30 GiB na Fase 6, sem provisionamento real. 32 KiB/s por 24h são ~2.6 GiB brutos antes de compressão,
índices/WAL/grace. Somar imagens, banco, dados auxiliares Nextcloud e head/WAL Prometheus.
Retenção não impede disco cheio. Não há limites arbitrários de CPU/RAM dos containers.

## Rede, mounts e risco

observability é bridge internal; Alloy também entra em backend para probes.
Nextcloud mantém backend/frontend. A revisão da Fase 6 prepara Grafana em
**0.0.0.0:3000:3000**, somente IPv4, com SG restrito a HTTP_ALLOWED_CIDRS.
Grafana mantém observability e acrescenta grafana_access, bridge não interna
com IPv6 desabilitado e nenhum outro serviço. A porta 3000 é validada em Ansible
para coincidir com Terraform e as URLs. Demais componentes
não publicam portas. Rede interna restringe egress/IMDS; não há credenciais ou permissões
AWS novas. Validar a restrição na VM; ela não isola processos privilegiados no host.

| Serviço | Mount | Finalidade/exposição |
| --- | --- | --- |
| Node Exporter | /proc → /host/proc:ro | CPU/memória/stat/boot/mountinfo; metadata host legível ao UID |
| Node Exporter | /sys → /host/sys:ro | Metadata kernel/dispositivos/disco |
| Node Exporter | / → /host/root:ro,rslave | statfs real; rslave recebe mounts sem propagar ao host; leitura ampla conforme UID |
| Alloy | data-root/containers → /var/lib/docker/containers:ro | Logs JSON e metadata nesse diretório; UID root para arquivos root-owned |

Node Exporter é nobody, sem host PID/network/capabilities. Alloy usa UID 0/GID 473,
cap_drop ALL, no-new-privileges e rootfs readonly, só estado local gravável. Sem
privileged ou socket. Socket ro ainda permite chamadas que alteram o daemon; arquivos
evitam acesso ao plano de controle, mas não isolam logs de outros clientes no mesmo
host: filtro ocorre depois da leitura. Logs podem conter dados sensíveis.

Depende de Docker rootful/Linux e json-file/attrs configurados por serviço. Docker
documenta arquivos internos ao daemon: nunca escrever/rotacionar por ferramentas
externas; verificar coleta após upgrades/rotação. Data-root diferente exige variável
nextcloud_stack_docker_log_directory, diretório existente/root-owned terminando em
containers; Compose não cria pasta vazia silenciosamente.

Prometheus/Node Exporter/Grafana têm healthchecks HTTP. Loki distroless e Alloy sem
cliente HTTP: Ansible checa /ready e /-/ready por IP bridge inspecionado localmente,
sem publicar portas. verify-config não é usado como falso healthcheck. Ansible também
valida Grafana, datasources e dashboard; readiness não prova ingestão de logs/métricas.

Após aplicação autorizada, abrir `http://IP_PUBLICO_ATUAL:3000` a partir do
CIDR administrativo. O workflow descobre o IP via outputs runtime e mostra a URL;
Ansible renderiza também GF_SERVER_ROOT_URL. Túnel SSH não é requisito.
Autenticação continua obrigatória. HTTP sem TLS deixa tráfego e credenciais sem
criptografia; CIDR restrito não substitui TLS. Probes no host não comprovam
acessibilidade externa. Não liberar TCP/3000 ao runner para simular esse teste.
Diagnóstico reproduzido, impacto e procedimento em
[acesso Grafana](../docs/grafana-browser-access.md).

## Ansible e validação

nextcloud_stack inclui monitoring.yml/validate-monitoring.yml. Instala python3-requests
para consulta local Docker; diretórios 0755/arquivos 0644 não secretos, Host blackbox
renderizado, secret Grafana protegido, Compose único. Pull missing/up recreate auto/
wait preservam volumes. Arquivos alterados acionam restart só da observabilidade
afetada, dependencies=false; paths bind iguais não detectam conteúdo alterado pelo
Compose. Primeiro deploy pode ter restart adicional. Rerun idêntico não reinicia.
Não reiniciar aplicação por troca de dashboard. Nome single replica segue project-service-1.

Compose manual exige blackbox.yml renderizado; caminho normal é Ansible. Compose
config valida o modelo, não mounts existentes ou execução.

```bash
python -m pip install -r ansible/tests/requirements-static.txt
python monitoring/tests/check_static.py
python monitoring/tests/check_static.py --promtool /path/promtool --loki /path/loki --alloy /path/alloy
```

Validadores oficiais/checksummed Windows ficam em .validation-tools ignorado. Foram
executados promtool check config/rules/test rules/parser PromQL, Loki -verify-config,
Alloy validate/fmt, YAML/JSON/Jinja, Compose config e contratos anteriores, sem iniciar
servidores. Syntax-check/lint Ansible e operação real dependem de Linux/VM. Comandos,
arquivos e procedimento operacional em [Fase 5](../docs/phase-5.md).

Fontes: [Node Exporter](https://github.com/prometheus/node_exporter),
[Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/),
[Loki retention](https://grafana.com/docs/loki/latest/operations/storage/retention/),
[Alloy Blackbox](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.blackbox/),
[Alloy files](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.file/),
[Docker JSON logs](https://docs.docker.com/engine/logging/drivers/json-file/),
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/).
