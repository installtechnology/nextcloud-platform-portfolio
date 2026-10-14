> Estado atual: deploy automatizado validado na AWS, conforme a [Fase 6](../docs/phase-6.md).
> Os registros de testes/limitações das entregas originais abaixo são históricos;
> consultar o relatório atual para distinguir validações concluídas de pendências.

# Stack Nextcloud — Fases 3–5

Compose com Nextcloud, PostgreSQL e Redis preservados, S3 AWS obrigatório como Primary Object Storage e observabilidade local da Fase 5. Não
configura backup/DR, proxy, domínio, HTTPS ou workflows.
Nenhum container foi executado em VM nesta entrega.

## Versões e arquitetura

| Serviço | Imagem fixada | Função |
| --- | --- | --- |
| Nextcloud | 34.0.4-apache | HTTP nativo da imagem oficial |
| PostgreSQL | 17.11-bookworm | Banco exclusivo do Nextcloud |
| Redis | 7.4.8-bookworm | Cache e file locking |

compose.yml fixa também os digests SHA-256 de índices multiarch. Tags/manifests
retornaram HTTP 200 no registry oficial, com amd64/arm64; não é teste operacional.
Sem latest. Patches não entram automaticamente: revisar tag/digest e testar.
Major PostgreSQL não pode mudar sobre PGDATA existente sem migração planejada.
Fontes: [Nextcloud](https://github.com/nextcloud/docker),
[PostgreSQL](https://github.com/docker-library/postgres),
[Redis](https://github.com/redis/docker-library-redis).

backend é bridge internal; PostgreSQL/Redis não publicam portas. Nextcloud também
usa frontend e publica HTTP 80 por default (bind/porta configuráveis). SG continua
restringindo acesso. Não há privileged, host network ou Docker socket montado.

postgres_data monta /var/lib/postgresql/data; nextcloud_html monta /var/www/html,
incluindo config/apps/cache e dados auxiliares locais; conteúdo dos usuários fica no S3. São named volumes temporários, prefixados pelo
COMPOSE_PROJECT_NAME. Sobrevivem ao restart dos containers, **não à perda da EC2/EBS**.
Não são backup. Não mudar project name ou remover/prunar volumes para testar
idempotência. Ansible usa remove_volumes=false e não executa down/prune.
Redis usa usuário redis, cap_drop ALL, root filesystem readonly e tmpfs; save e
appendonly ficam desativados. A senha vai para configuração temporária, não argv
do servidor. Nextcloud/PostgreSQL preservam bootstrap/drop de privilégios oficial,
necessário para preparar volumes. Não foram impostos limites arbitrários de CPU/RAM.

## Instalação reproduzível

O [entrypoint oficial](https://github.com/nextcloud/docker/blob/master/docker-entrypoint.sh)
executa occ maintenance:install com variáveis DB/admin só na primeira inicialização;
não depende do navegador e não reinstala volumes existentes. Mudanças no admin env
não redefinem senha de instalação existente. Os hooks pre-installation e before-starting copiam a config
complementar se diferente, após os arquivos oficiais. S3 entra antes de occ install;
reexecuções validam a identidade de storage antes de copiar. Não montar arquivos em config/
antes desse bootstrap, pois tornaria o diretório não vazio e impediria cópia oficial.

APCu é memcache.local; Redis é memcache.distributed/memcache.locking. Host/port/auth
Redis vêm do redis.config.php oficial e REDIS_*; trusted_domains vêm do env runtime,
permitindo atualizar IP com recriação do container sem apagar config/banco. Proxies
ficam ausentes até existir proxy real aprovado. Não adicionar trusts genéricos.

PostgreSQL usa secret administrativo próprio. Seu init script cria role da aplicação
NOSUPERUSER/NOCREATEDB/NOCREATEROLE e transfere ownership do banco configurado. Esse
script roda somente com PGDATA vazio. Nextcloud nunca recebe o secret administrativo
PostgreSQL. Mudar env não rotaciona senha dentro de banco existente.
Não mudar nomes de banco/user/project em volumes existentes sem migração. O probe
aborta se banco/user efetivos da config Nextcloud divergirem dos valores runtime.

## Secrets obrigatórios

Somente .env.example com placeholders é versionado. Ansible gera .env no host com
modo 0600, contendo nomes/trusted domains/bind/porta e referências a secret files.
As credenciais chegam no controlador via env segura ou vars equivalentes com Vault:

| Entrada runtime | Uso |
| --- | --- |
| PLATFORM_HOST | Endereço SSH e trusted domain inicial |
| NEXTCLOUD_ADMIN_USER | Admin inicial Nextcloud |
| NEXTCLOUD_ADMIN_PASSWORD | Senha admin Nextcloud |
| POSTGRES_ADMIN_PASSWORD | Administração PostgreSQL, isolada da aplicação |
| POSTGRES_PASSWORD | Conta PostgreSQL do Nextcloud |
| REDIS_PASSWORD | Autenticação Redis |

Os quatro passwords devem ter 32–256 caracteres A-Z/a-z/0-9/_/-, gerados aleatoriamente
fora do repo. A restrição evita interpretação na configuração Redis. Nunca passar
senhas em argumentos CLI/logs. Manter os mesmos valores nas execuções seguintes.
As senhas PostgreSQL administrativa e da aplicação devem ser diferentes.
Banco/user/project/bind/porta são parametrizados em defaults não secretos.

No host, secrets/ é root:root 0700 e arquivos são 0444 para leitura por www-data/redis
dentro dos mounts. A travessia do diretório protege acesso no host; somente serviços
autorizados recebem cada secret. Compose local faz bind de arquivos, não é cofre
Swarm/encriptação: root/operador Docker ainda pode lê-los. No_log protege tarefas
sensíveis. Checksums impedem rotação acidental: divergência aborta antes de gravar.
Preservar credenciais fora da VM também. Rotação exige procedimento separado.

Para execução manual futura, preparar env local a partir do exemplo e arquivos
_FILE_PATH externos reais. O exemplo não é configuração aplicável. Ansible é o
caminho preferido; não imprimir config expandido com credenciais.

## Ansible e validação Linux futura

Instalar requirements-control.txt e a collection fixada community.docker 5.3.0.
bootstrap.yml mantém só Fase 2; deploy.yml adiciona nextcloud_stack em play separado.
O Compose tem fonte única, copiada da pasta docker/. Ver [Ansible](../ansible/README.md).

```bash
cd ansible
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i inventory/hosts.yml.example playbooks/deploy.yml --syntax-check
ansible-lint playbooks roles
# Só com VM autorizada, PLATFORM_HOST/secrets injetados e SSH externo pronto:
ansible-playbook -i inventory/hosts.yml.example playbooks/deploy.yml
```

Collection usa Compose Plugin >=2.18: pull missing, up present/recreate auto e wait
(600s parametrizados). Command é somente occ status/probe PHP, changed_when=false e
failed_when explícitos. Scripts locais tratam init PostgreSQL/config Redis/hook;
não executam provisionamento por script remoto. Nenhum shell Ansible de instalação.

Healthchecks: PostgreSQL readiness TCP; Redis PING autenticado; Nextcloud status.php
com installed=true e sem maintenance/upgrade pendente. Além de esperar os três,
Ansible verifica occ status, SELECT 1 pelo Nextcloud, Redis AUTH/PING/cache e HTTP
local. Readiness sozinho não valida login da aplicação. Não executados em VM ainda.

Validação sem containers (parsers disponíveis):

```bash
python ansible/tests/check_static.py
python docker/tests/check_static.py
python docker/tests/check_static.py --docker-cli /path/to/docker
```

## Teste operacional pendente

1. Ansible prepara VM e stack; registrar healthchecks/probes sem expor credenciais.
2. Acessar http://IP; login, criação de usuário e upload de arquivo fictício.
3. Reiniciar containers sem remover volumes; confirmar banco/config/arquivo.
4. Reexecutar Ansible com mesmos secrets/project/volumes; confirmar instalação
   preservada e segunda execução sem alterações indevidas.
5. Registrar CPU/RAM/disco/concorrência e tempo de instalação. Default t3.medium
   não foi alterado/certificado para a stack final; disco 30 GiB (default atualizado na Fase 6) exige acompanhar uso.

HTTP é apenas demo temporária com dados fictícios, sem TLS. Não há cron dedicado;
background jobs permanecem no modo da instalação, a revisar para uso contínuo.
Fase 4 configura S3 Primary Object Storage para instalação nova: **não habilitar sobre uploads locais sem
migração explícita ou ambiente novo autorizado**. Dados não se convertem sozinhos.
Fase 7 fará backup banco/config e Fase 8 DR; nenhum deles foi implementado aqui.

## Primary Object Storage AWS S3 — instalação nova

Obrigatório informar NEXTCLOUD_S3_BUCKET (output persistent.data_bucket_name)
e NEXTCLOUD_S3_REGION (output persistent.bucket_region), ou variáveis Ansible
nextcloud_stack_s3_bucket/nextcloud_stack_s3_region. Não usar bucket de backups
ou state. Bucket deve existir, na mesma região, ser exclusivo deste Nextcloud e
inicialmente vazio. Os exemplos contêm somente placeholders.

platform.config.php configura OC\Files\ObjectStore\S3, HTTPS, region explícita
e endereçamento virtual host regional padrão AWS. Não há key, secret, credentials
ou mount de ~/.aws. A versão stable34 usa o default provider do SDK AWS sem shared
config files; este obtém/renova credenciais temporárias do Instance Profile via
IMDSv2. EC2 exige tokens e hop limit 2 para bridge Docker. O host/container precisa
alcançar 169.254.169.254 e o endpoint regional S3. Este template usa endpoints
padrão amazonaws.com; outras partições/endpoints exigem revisão antes de implantação.
Não definir variáveis AWS de credenciais no Compose nem fornecer chaves ao Nextcloud.

verify_bucket_exists=false evita o ramo do stable34 que tenta criar bucket ausente.
Terraform é o proprietário; role não possui CreateBucket. O probe de integração faz
HeadBucket autenticado com o cliente S3 real, somente leitura, após instalação;
uma falha aborta a validação Ansible e exige diagnóstico, sem fallback para disco.

O hook oficial pre-installation executa antes de occ maintenance:install. O mesmo
hook before-starting mantém config em rerun. storage-guard.php grava apenas identidade
não secreta (classe/bucket/região/opções) em config/.platform-storage.json. Recusa
instalação local existente, ausência de marcador em instância instalada ou troca
de bucket/região/opções; não apagar o marcador para contornar a proteção. Nenhuma
migração/cópia de arquivos locais ou adoção de instalação existente está implementada.

Nextcloud usa chaves de objeto próprias como urn:oid:<id>, sem prefixo nextcloud-data/;
filenames/diretórios/permissões vivem no PostgreSQL. Não editar objetos diretamente.
Versionamento mantém versões/delete markers; role de aplicação não exclui versões.
Driver v34.0.4 envia ACL private em uploads/cópias; por isso dados usam
BucketOwnerPreferred e IAM PutObjectAcl condicionado a private. Bloqueio público
permanece integral; backups usam BucketOwnerEnforced. Sem patches no driver.
S3 sozinho não é backup completo, e restore histórico coordenado não está resolvido.
Banco/config/apps/marker continuam locais; perda do host pode impedir recuperar a
instância mesmo com conteúdos preservados. Backup/DR pertencem a fases posteriores.

Validação local: `python tests/check_storage.py <php-cli>` testa instalação nova,
rerun, recusa de instalação local e troca de bucket/região, sem rede/AWS.
Teste futuro em VM: verificar probe, criar usuário/upload/download, conferir objetos
no bucket dados e ausência no bucket backups, reiniciar/reexecutar sem perda; nunca
remover volumes para provar idempotência. Esses testes não foram executados.

Fontes: [Primary storage](https://docs.nextcloud.com/server/latest/admin_manual/configuration_files/primary_storage.html),
[S3 stable34 e default credential provider](https://github.com/nextcloud/server/blob/stable34/lib/private/Files/ObjectStore/S3ConnectionTrait.php),
[ordem dos hooks](https://github.com/nextcloud/docker/blob/master/34/apache/entrypoint.sh).

ACL: [driver exato v34.0.4](https://github.com/nextcloud/server/blob/v34.0.4/lib/private/Files/ObjectStore/S3ObjectTrait.php),
[AWS Object Ownership](https://docs.aws.amazon.com/AmazonS3/latest/userguide/about-object-ownership.html).

## Observabilidade integrada — Fase 5

prometheus/grafana/loki/alloy/node-exporter foram adicionados ao mesmo compose.yml,
com tags/digests fixados; tabela em [Monitoring](../monitoring/README.md). Nenhuma
imagem/configuração S3/instalação da Fase 4 foi alterada. Aplicação recebe apenas
logging json-file rotacionado, com attrs dos labels Compose; redes permanecem iguais.

observability é rede internal; Alloy também usa backend para probes. Prometheus,
Loki, Alloy e exporter sem ports. Grafana publica 0.0.0.0:3000:3000 exclusivamente
em IPv4. A proteção externa exige o SG TCP/3000 restrito a HTTP_ALLOWED_CIDRS;
0.0.0.0 isoladamente não é controle de acesso. observability continua internal;
somente Grafana também usa grafana_access, bridge não interna sem IPv6.
GRAFANA_PUBLIC_HOST é renderizado de ansible_host, descoberto pelos outputs runtime.
GRAFANA_HTTP_PORT/nextcloud_stack_grafana_http_port devem ser 3000 neste contrato.
A URL raiz acompanha o IP atual sem trocar datasources, dashboards ou volumes.
Diagnóstico e resultado aplicado em [acesso Grafana](../docs/grafana-browser-access.md). GRAFANA_ADMIN_USER obrigatório e password via
GRAFANA_ADMIN_PASSWORD_FILE_PATH/secret seletivo, nunca default fixo ou senha no env.

Volumes prometheus_data/grafana_data/loki_data/alloy_data são locais/descartáveis,
fora de backup/DR. Prometheus 48h/1GB de blocos; Loki 24h, sem S3; Docker logs 5m/3.
Não são quotas totais: medir WAL/head/índices, imagens, banco e disco livre.

Node Exporter nobody monta /proc,/sys,/ readonly para métricas reais do host.
Alloy UID0/GID473 lê data-root/containers readonly, sem socket; cap_drop ALL,
no-new-privileges, rootfs readonly. Read-only não elimina leitura de dados sensíveis;
detalhes/finalidade dos mounts e alternativa socket em Monitoring. Sem privileged.

Healthchecks HTTP em Prometheus/Grafana/Node Exporter. Loki distroless/Alloy sem
cliente HTTP recebem readiness por inspeção Docker + uri Ansible, não por fake
verify-config. Blackbox no Alloy evita serviço extra; seus jobs normalizados
nextcloud/postgres/redis chegam via remote_write ao Prometheus interno.

Compose manual exige monitoring/alloy/blackbox.yml renderizado previamente do .j2;
Ansible faz isso com Host do trusted domain, sem modificar Nextcloud. Não apontar
Compose diretamente ao template cru. config --quiet não comprova mounts/execução.

Dashboard/ALERTS provisionados; PromQL e regras testadas offline. Render, coleta,
logs, alerta por parada real e consumo ainda dependem de VM. Não há workflow,
notificações, backup/DR ou mudança de instance_type.
