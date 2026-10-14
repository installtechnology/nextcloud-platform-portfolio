> Estado atual: deploy automatizado validado na AWS, conforme a [Fase 6](../docs/phase-6.md).
> Os registros de testes/limitações das entregas originais abaixo são históricos;
> consultar o relatório atual para distinguir validações concluídas de pendências.

# Bootstrap Ubuntu — Fase 2

Instala pacotes básicos, Docker Engine, containerd, Buildx e **Compose Plugin
moderno**. Nenhum container, imagem da aplicação, volume ou serviço Nextcloud,
PostgreSQL, Redis, Prometheus, Grafana, Loki ou Alloy é criado. Não há backup
ou persistência definitiva. O daemon Docker é o único serviço configurado.

## Pré-requisitos

Controlador Linux/macOS com Python >=3.11, OpenSSH e requirements-control.txt.
Bootstrap usa somente builtins. Deploy da Fase 3 usa community.docker fixada em requirements.yml. Host Ubuntu 22.04/24.04 LTS limpo,
systemd, Python3, usuário operacional existente, SSH e sudo. O host precisa
acessar mirrors APT e download.docker.com via HTTPS. A role aborta se houver
pacotes Docker conflitantes; não remove automaticamente instalações existentes.

Preparar controlador Linux (não executado neste Windows):

```bash
cd ansible
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-control.txt
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
```

Windows nativo não é controlador suportado. Ver
[limitações oficiais](https://docs.ansible.com/projects/ansible/latest/os_guide/intro_windows.html).
Nenhuma distribuição WSL, VM ou infraestrutura foi criada nesta entrega.

## IP dinâmico e SSH

inventory/hosts.yml.example obtém ansible_host de PLATFORM_HOST e usuário de
PLATFORM_SSH_USER (default ubuntu). Não há IP fixo. Futuramente GitHub Actions
lerá public_ip do state runtime autenticado e exportará esse valor. É possível
usar o exemplo diretamente ou copiar para hosts.yml local, ignorado pelo Git.

Exemplo futuro, com host existente e autorização de configuração:

```bash
export PLATFORM_HOST="$(terraform -chdir=../terraform/aws/runtime output -raw public_ip)"
export PLATFORM_SSH_USER=ubuntu
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
ansible-playbook -i inventory/hosts.yml.example playbooks/bootstrap.yml --private-key /path/outside/repository/operator-key
```

OIDC autentica AWS, não SSH. A chave privada correspondente à pública EC2 deve
ficar fora do Git, em ssh-agent ou --private-key. Em CI futuro poderá vir de
GitHub Secrets, com limpeza após execução e sem logging do conteúdo.
O SG deve autorizar o IP do controlador; não abrir SSH mundialmente.

Host key checking fica ativo. Antes da primeira conexão, verificar fingerprint
por canal confiável/console e registrar no known_hosts. Não desabilitar a
verificação nem aceitar ssh-keyscan sem comparação confiável. Verificar nova
host key depois de recriar EC2. Em filesystem onde ansible.cfg possa ser
ignorado por permissões, ANSIBLE_CONFIG explícito garante a configuração correta.

## Roles e diretórios

common: cache APT com validade de 3600s, ca-certificates, curl, python3-apt e
python3-debian; grupo nextcloud-platform e usuário existente com append=true.
Cria diretórios root:nextcloud-platform 0750, sem escrita coletiva:

```text
/opt/nextcloud-platform/
├── docker/
├── monitoring/
├── data/
└── backups/
```

Usuário, grupo, raiz, subdiretórios e pacotes são parametrizados nos defaults.
Não cria mounts, discos adicionais, volumes, dumps ou uploads. O playbook usa
become para configurar arquivos; fases posteriores definirão UIDs/permissões
específicas de cada container.

docker: chave ASCII oficial em /etc/apt/keyrings/docker.asc, repositório deb822
com signed_by, arquitetura/codename via facts, pacotes oficiais e systemd.
Método baseado na [documentação Docker](https://docs.docker.com/engine/install/ubuntu/).
Sem apt-key, curl|sh, docker-compose standalone ou upgrades state=latest.
Pacotes usam state=present; novas VMs recebem stable disponível, sem versão
congelada. docker_packages permite pinning explícito/upgrade controlado futuro.

docker_allow_operational_user=false não concede nova adesão ao grupo docker.
Se necessário, usar -e docker_allow_operational_user=true. O usuário existente
é adicionado com append=true. Acesso ao grupo docker equivale a root; become
é o padrão inicial. false não revoga adesão preexistente. A sessão SSH é
renovada após setup para atualizar grupos, sem alterar recursos persistentes.

Validação usa service_facts/assert, docker info e docker compose version --short.
As duas consultas CLI usam command com changed_when=false: não há builtin
equivalente para consultar daemon/versão do plugin. Não usa hello-world, pull
ou compose up. Opcionalmente valida docker info como usuário autorizado.

## Validação e idempotência

Em controlador Linux, sem conexão à VM:

```bash
export PLATFORM_HOST=unused-for-syntax-check
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
ansible-playbook -i inventory/hosts.yml.example playbooks/bootstrap.yml --syntax-check
ansible-lint playbooks/bootstrap.yml roles/common roles/docker
```

Idempotência projetada por state=present/directory/started, append=true, cache
com validade, chave não rebaixada a cada execução e probes sem changed. Não há
reinício forçado, duplicação de usuários/grupos ou instalação via comandos shell.
Com VM futura autorizada, executar duas vezes consecutivas e guardar recaps:
esperar changed=0 na segunda, Docker ativo e Compose respondendo. Refresh do
cache após validade ou alterações externas podem gerar mudanças legítimas.
Essa evidência em VM ainda está pendente.

--check não substitui teste real. Em host limpo pode falhar por grupos/pacotes
que só existiriam após aplicar. Tarefas Docker dependentes do repositório e
probes são omitidas em check mode. Não anunciar dry-run parcial como bootstrap
ou validação completa.

Fallback estático multiplataforma, sem SSH:

```bash
python -m pip install -r tests/requirements-static.txt
python tests/check_static.py
```

Verifica sintaxe YAML (inclusive chaves duplicadas)/Jinja, roles/imports,
inventário dinâmico, módulos e limites da fase. **Não substitui** syntax-check,
lint ou Ubuntu real. Não há Molecule, pois não há ambiente de containers para
executá-lo e esta fase não precisa adicionar essa complexidade.

## Deploy da Fase 3 (opcional e separado)

bootstrap.yml é preservado sem depender da collection. deploy.yml importa bootstrap para preparação e executa em play separado common (mesma role reutilizada) e nextcloud_stack; não executa Terraform nem adiciona configuração cloud.
Instalar no controlador Linux a collection antes do syntax-check de deploy:

```bash
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i inventory/hosts.yml.example playbooks/deploy.yml --syntax-check
ansible-lint playbooks roles
```

Execução futura com VM autorizada, chave externa e PLATFORM_HOST configurado:
injetar NEXTCLOUD_ADMIN_USER, NEXTCLOUD_ADMIN_PASSWORD, POSTGRES_ADMIN_PASSWORD,
POSTGRES_PASSWORD e REDIS_PASSWORD de fonte segura (ou variáveis nextcloud_stack_*
equivalentes via Ansible Vault). Não passar senhas como argumentos CLI.
Todos os quatro passwords devem manter valores estáveis, 32–256 caracteres
A-Z/a-z/0-9/_/-. A role aborta se secrets existentes não corresponderem; não
rotaciona credenciais nem reinstala banco/app em rerun.

```bash
ansible-playbook -i inventory/hosts.yml.example playbooks/deploy.yml
```

nextcloud_stack usa copy/template com permissões/no_log, módulos Compose para
pull missing e up present/recreate auto/wait, e command somente leitura para
occ status/probe PHP. Compose não é duplicado em templates: usa docker/compose.yml.
A instalação inicial é controlada pelo hook before-starting após configurar S3;
o auto-install nativo é desativado para evitar dois instaladores. installed=true
não reinstala e estado parcial incompatível bloqueia sem limpeza automática. S3 é obrigatório na Fase 4; proxies/HTTPS não são configurados.
Arquitetura, persistência temporária, segredo PostgreSQL administrativo separado
e teste operacional em [docker/README.md](../docker/README.md).

Os statements de ausência de containers desta página descrevem bootstrap.yml
da Fase 2; deploy.yml é a entrada explícita da Fase 3. Terraform e
containers não foram executados neste workspace. Docker CLI/Compose Plugin
portáteis foram usados somente para config, sem daemon; sintaxe Ansible/lint
ainda depende de Linux. Check mode da stack é parcial e não substitui o deploy.

## Entradas S3 da Fase 4

Fornecer NEXTCLOUD_S3_BUCKET e NEXTCLOUD_S3_REGION no controlador (ou variáveis
nextcloud_stack_s3_bucket/region), a partir de outputs persistent.data_bucket_name
e persistent.bucket_region. São metadados não secretos; o .env host inclui somente
esses nomes, nunca credenciais AWS. PLATFORM_HOST continua vindo do output runtime.public_ip;
nenhum inventário/IP/chave privada foi fixado. SSH usa chave externa/host key verificada.

deploy.yml prepara instalação nova usando S3 antes de instalar Nextcloud. IMDSv2
e IAM Instance Profile fornecem credenciais dentro da EC2; OIDC do controlador
não é repassado para o app. Guard recusa instalação local ou mudança de bucket.
bootstrap.yml permanece somente common/docker. Probe de integração adiciona
HeadBucket autenticado, sem escrever/excluir objetos. Não há backup ou migração.

## Observabilidade — Fase 5

deploy.yml/nextcloud_stack agora exigem GRAFANA_ADMIN_USER/GRAFANA_ADMIN_PASSWORD
no controlador ou vars nextcloud_stack_grafana_admin_user/password via Vault.
Senha 32–256 caracteres alnum/_/-, estável entre reruns; sem valor default.
Secret Grafana usa as mesmas proteções/checksum/no_log dos demais, montado só nele.
As quatro senhas da aplicação continuam obrigatórias; total de cinco nesta fase.

nextcloud_stack_grafana_http_port permanece 3000, validado contra a regra Terraform.
Grafana publica somente IPv4, com origem restrita pelo SG a HTTP_ALLOWED_CIDRS.
GRAFANA_PUBLIC_HOST vem automaticamente de ansible_host; o destino não é fixado.
Alterar a porta exigiria revisar conjuntamente Terraform, Compose e URLs.

nextcloud_stack_docker_log_directory default /var/lib/docker/containers recebe
data-root alternativo Docker rootful. Diretório deve existir/root-owned; não criar
um mount vazio para simular logs. Nenhum IP ou chave privada foi fixado.

monitoring.yml instala python3-requests para docker_container_info local, cria
diretórios não secretos 0755/arquivos 0644, copia arquivos versionados e renderiza
blackbox Host do primeiro trusted domain. S3/config/instalação Nextcloud preservados.
Model/monitoring files têm fonte única em monitoring/; não duplicar em roles.

Pull missing/up recreate auto/wait preservados. Conteúdo de bind mudando não
força recreate pelo Compose: resultados copy/template selecionam restart apenas
dos serviços de observabilidade afetados, dependencies=false. Primeiro deploy
pode reiniciar após copiar; rerun idêntico não reinicia. Não afeta aplicação ao
mudar dashboard. validate-monitoring.yml aguarda readiness interno via IP bridge,
binding IPv4 efetivo do Grafana, API no host, datasources e dashboard.
Probes dentro da EC2 não comprovam acesso externo pelo CIDR administrativo. Single replica project-service-1;
check mode continua parcial, sem Docker up ou probes.

Controlador Linux deve executar syntax-check/lint com collection 5.3.0 instalada;
Windows desta entrega só executou YAML/Jinja/contratos/seleção de restarts offline.
Idempotência real e recaps de duas execuções permanecem pendentes. Acesso, mounts,
retenção e risco em [Monitoring](../monitoring/README.md); procedimento em
[Fase 5](../docs/phase-5.md). Bootstrap.yml segue só common/docker da Fase 2.

## Integração GitHub Actions — Fase 6

deploy.yml do workflow reutiliza playbooks/deploy.yml, sem duplicar roles. Runner
Ubuntu instala requirements-control/collection; syntax-check e ansible-lint
(profile safety, config da raiz) precedem Terraform apply. Não houve execução
Linux local nesta fase: WSL sem distribuição e Docker daemon indisponível.

Inventário JSON efêmero recebe IP/usuário dos outputs runtime. GITHUB_ENV fornece
PLATFORM_HOST/PLATFORM_SSH_USER/NEXTCLOUD_S3_BUCKET/NEXTCLOUD_S3_REGION. Senhas só
no ambiente do passo Ansible, protected Secrets; sem extra-vars contendo senha.
SSH_PRIVATE_KEY vira arquivo 0600; SSH config exige StrictHostKeyChecking yes,
IdentitiesOnly e known_hosts cuja chave Ed25519 foi comparada a fingerprint do
console EC2 autenticado. Chave pública deve corresponder. Erro/missing key aborta.

O controller aplica common/docker/nextcloud_stack e probes existentes: status
Nextcloud + integração PostgreSQL/Redis/S3, readiness Prometheus/Loki/Alloy/
Node Exporter e health/datasources/dashboard Grafana. Apenas mode=apply conecta.
Cleanup always revoga somente ingress desta execução e remove arquivos efêmeros.
Regra no-handler para cache APT é explicitamente justificada: precisa atualizar
antes de instalar Docker, não no encerramento do play. Contratos de secrets e
proteção contra rotação acidental permanecem. Idempotência real exige dois runs
autorizados. Detalhes em [Fase 6](../docs/phase-6.md).
