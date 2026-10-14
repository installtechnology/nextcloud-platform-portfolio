# Decisões da Fase 1 — arquitetura revisada

1. Três roots/states independentes: bootstrap administra GitHub OIDC/IAM;
   persistent mantém S3/dados e futuramente state; runtime contém só recursos
   descartáveis da aplicação. Destroy operacional futuro atua somente em runtime.
2. OIDC/provider/role saíram completamente de runtime. Bootstrap não depende de
   dados/outputs/state do runtime e não consulta o bucket da aplicação para criar
   seu IAM. Provider existente do cliente é referenciado; provider criado e role
   têm prevent_destroy. Remover bootstrap é operação administrativa separada.
3. Trust GitHub exige subject exato de repo+environment/branch e audience
   sts.amazonaws.com. Role inicial possui somente leitura de backups; políticas
   mínimas de runtime/backend serão implementadas na Fase 6. Sem AdministratorAccess
   ou permissões de autoalteração do IAM administrativo. Administrador SSO/federado
   faz primeira criação e recuperação de problemas na autenticação.
4. O requisito original de sobreviver somente S3 foi revisado explicitamente:
   sobrevivem também OIDC/IAM bootstrap e, no futuro, bucket de state. Assim a
   automação continua autenticável depois de destruir a EC2, sem rebootstrap a
   cada deploy/DR. A role GitHub ainda precisa das permissões operacionais futuras.
5. State local continua somente nesta etapa. Estratégia futura: bucket S3 dedicado
   de state, gerenciado por persistent e separado dos dados Nextcloud; chave por
   conta/projeto/ambiente/root, workspace default. Deploy/destroy/DR compartilham
   exatamente o mesmo state runtime. Nenhum backend habilitado ou state migrado.
6. Backend S3 usará use_lockfile=true (Terraform >=1.10), lock objeto .tflock.
   Não usar DynamoDB: locking nativo atende o projeto e DynamoDB locking está
   depreciado. Não introduzir tabela ou KMS dedicada. Permitir Get/Put no state,
   Get/Put/Delete só no lock; sem DeleteObject para state. CI não acessa states
   administrativos; EC2 não acessa bucket de state.
7. Bootstrap do backend: sessão SSO com state local protegido cria persistent
   incluindo bucket dedicado, depois configura IAM bootstrap; só após bucket
   existir habilitar backend e migrar states por root, com revisão/autorização.
   Runners usam backend já existente. State de persistent pode residir no bucket
   que ele próprio gerencia após migração. Preservar bucket/versões e recuperação
   administrativa fora do runtime. Sequência detalhada em terraform-state.md.
8. S3 de dados mantém prevent_destroy, force_destroy=false, versionamento,
   SSE-S3, BucketOwnerEnforced, bloqueio público e TLS obrigatório. Sem lifecycle/
   expiração/exclusão automática. As mesmas proteções serão exigidas para state.
   prevent_destroy só funciona enquanto recurso/bloco permanece na configuração;
   não substitui IAM/revisão. Retenção indefinida pode aumentar custo de versões.
9. instance_type parametrizado, default alterado explicitamente pelo usuário antes da Fase 2; EBS gp3 12 GiB mantido.
   Não é sizing definitivo da stack. Medir CPU/memória/disco/concorrência para
   Nextcloud, PostgreSQL, Redis, Prometheus, Grafana, Loki e Alloy antes de definir
   tamanho final, incluindo picos de backup/restore. Não aumentar agora.
10. AMI Ubuntu Server 24.04 LTS via SSM Canonical na região configurada. Não há
    AMI/account/região fixos de cliente. Atualização de current pode substituir
    EC2; revisar plano e considerar serial de imagem fixado em operação prolongada.
11. EBS criptografado com chave padrão, delete_on_termination; nenhum snapshot,
    EIP, NAT, ALB, RDS ou serviço caro adicional. T3 standard evita custos de
    créditos unlimited, com possível redução de desempenho por crédito esgotado.
12. SSH exige /24 ou mais restrito; HTTP exige CIDRs explícitos, sem portas públicas
    de banco/cache/monitoramento. Saída IPv4 aberta para pacotes/imagens/APIs.
    IMDSv2 requerido, hop limit 2 para containers bridge; containers podem acessar
    a identidade EC2, então o papel é restrito ao bucket exclusivo de dados (revisão Fase 4).
13. Revisado na Fase 4: IAM EC2 acessa somente bucket de dados, com exclusão de
    objetos atuais, sem DeleteObjectVersion. Política backup/restore separada, sem
    attachment à aplicação, só acessa backups. PutObject não impõe unicidade:
    timestamps únicos e escrita condicional serão responsabilidade da Fase 7.
    Backup é sempre manual, não ocorre em deploy/destroy/DR; destroy futuro deve
    abortar sem conjunto válido completo, mas seu workflow não foi implementado.
14. Restore histórico do banco/config não garante point-in-time dos objetos S3
    atuais; Fases 4/7/8 devem resolver versões/exclusões posteriores e registrar
    limitações. Nenhum teste real de aplicação ou DR foi executado.

Fontes primárias para as decisões de backend:
[HashiCorp S3/locking/IAM](https://developer.hashicorp.com/terraform/language/backend/s3),
[configuração de backend](https://developer.hashicorp.com/terraform/language/backend).
## Decisões da Fase 2

15. Default instance_type alterado para t3.medium por autorização explícita do
    usuário; única definição executável do valor está em variables.tf.
    Exemplos omitem instance_type para herdar default, testes comparam com a
    variável. Nenhuma VM real redimensionada ou infraestrutura criada.
    Parâmetro e sizing após medição continuam preservados.
16. Roles common/docker, somente builtins. Pacotes básicos + repositório APT
    oficial Docker deb822/signed_by; não usar script remoto, apt-key ou Compose
    standalone. Ubuntu LTS 22.04/24.04 e arquitetura x86_64/arm64 são validados.
    Pacotes state=present evitam upgrades repetidos; novas VMs obtêm stable
    disponível, então pinning determinístico continua decisão futura.
17. Usuário operacional deve existir. Grupos adicionados com append=true.
    Grupo Docker opt-in false por default (controle equivalente a root).
    Playbook usa sudo; não cria senha, chave ou usuário privilegiado implícito.
18. Diretórios vazios root:nextcloud-platform 0750 sob /opt, configuráveis.
    Não criar mounts, persistência definitiva, volumes ou lógica de backup.
    Arquivos/UIDs da stack pertencem às próximas fases.
19. Inventário recebe IP de PLATFORM_HOST; futuro runner exportará output
    public_ip do state runtime. OIDC não substitui SSH. Chave privada fora do Git,
    ssh-agent/--private-key, host key verificada e SG restrito ao controlador.
20. apt/group/user/file/deb822_repository/systemd_service fazem as mutações.
    command só consulta docker info e docker compose version com changed_when=false.
    A sessão SSH é renovada após configuração dos grupos. Nenhum container de
    teste, download de imagem de aplicação ou compose up é executado.
21. Idempotência projetada, ainda não comprovada em VM. Testes locais validam
    YAML/Jinja/imports/escopo; Ansible syntax-check/lint dependem de controlador
    Linux ausente neste Windows (WSL sem distro e Docker indisponível).
    Não instalar infraestrutura/distribuição só para simular validação. Molecule
    não foi adicionado. Check mode em host vazio é parcial e não é evidência de
    instalação nem substitui duas execuções consecutivas com recaps.

## Decisões da Fase 3

22. Somente três serviços: Nextcloud 34.0.4-apache, PostgreSQL 17.11-bookworm e
    Redis 7.4.8-bookworm. Tags e índices multiarch confirmados no registry; digests
    fixados em compose.yml. Não usar latest nem duplicar imagens em defaults.
    Atualizar tags/digests é operação explícita com revisão/testes de compatibilidade.
23. Nextcloud expõe HTTP nativo Apache; banco/cache ficam em bridge internal,
    sem ports. Nextcloud também tem rede frontend para HTTP/egress. Nenhum proxy,
    HTTPS, domínio, privileged, host network ou socket Docker. Nenhum serviço extra.
24. Named volumes locais nextcloud_html/postgres_data para config/apps/dados/banco
    enquanto host existe; não são backup nem sobrevivem ao destroy EC2/EBS.
    Não down/prune/remove_volumes, não mudar project name entre execuções. Redis
    sem persistência crítica, usuário redis/read-only/cap_drop/tmpfs/auth por secret.
25. Senhas via env segura do controlador ou Vault, sem defaults reais. .env host
    0600 contém nomes e paths, não passwords. Secret dir root 0700 e arquivos
    0444 permitem leitura aos usuários das imagens apenas dentro dos mounts
    autorizados. No host, acesso depende da travessia do dir; Compose não é um
    cofre cifrado e root/operadores Docker mantêm acesso. Checksums impedem
    rotação acidental. Senhas random alnum/_/- 32–256 chars evitam injeção em config.
26. PostgreSQL admin usa secret separado; aplicação tem role sem superuser/
    createdb/createrole, dona só do banco escolhido. Init script roda em PGDATA
    vazio. Redis tem wrapper local para config requirepass, pois não há variável
    oficial PASSWORD_FILE do servidor; evita segredo no argv do redis-server.
27. Instalação Nextcloud pelo entrypoint oficial/occ maintenance:install no
    primeiro bootstrap, sem reinstalar volumes existentes. Config complementar
    via hook oficial before-starting depois da cópia dos defaults, preservando
    redis.config.php/APCu oficial. Trusted domains runtime acompanham novo IP;
    sem trusted proxies genéricos. Probes usam OCP Server/IConfig/IDBConnection.
28. common/docker da Fase 2 preservados. bootstrap.yml preservado, sem nova collection;
    deploy.yml importa bootstrap e roda common/nextcloud_stack em play separado, reutilizando common para disponibilizar seus defaults e garantir diretórios sem duplicar definições. Collection community.docker
    5.3.0 fixada executa pull missing e up present/recreate auto/wait. Command
    só em occ status e integração PHP, changed_when=false/failed_when explícitos.
    Idempotência é desenho, não evidência de VM: banco/admin/volumes são preservados
    em rerun e credenciais divergentes abortam. DB/user config devem coincidir
    com runtime; mudança de nomes exige migração, não reinstalação silenciosa.
29. Testes locais: YAML/Jinja/templates/contratos, Compose config real por CLI
    portátil sem daemon, shell -n e PHP lint. Syntax-check/lint Ansible e todos os
    testes funcionais dependem de controlador/VM Linux; não executar cloud para
    contornar essa limitação. CLI/plugin/PHP isolados em .validation-tools ignorado.
30. Não alterar default t3.medium ou limites de CPU/RAM. Medir consumo futuro e
    uso do disco local. Sem S3 Primary Object Storage, backup, restore, DR,
    observabilidade ou workflows. Fase 4 precisará ambiente novo autorizado ou
    migração explícita de uploads locais; não converter storage automaticamente.

Fontes: [Nextcloud Docker](https://github.com/nextcloud/docker),
[PostgreSQL oficial](https://github.com/docker-library/postgres),
[Redis oficial](https://github.com/redis/docker-library-redis),
[módulo Compose](https://docs.ansible.com/projects/ansible/latest/collections/community/docker/docker_compose_v2_module.html).

## Decisões da Fase 4

31. Dois buckets persistentes independentes, nomes únicos configuráveis por ambiente:
    dados exclusivos do Primary Object Storage e backups manuais exclusivos de
    PostgreSQL/config/metadata. Terceiro bucket futuro exclusivo de Terraform state.
    Sem apply anterior, não há state/dados a migrar. Defaults EC2/EBS preservados.
32. Ambos têm prevent_destroy, force_destroy=false, versionamento Enabled, AES256,
    BucketOwnerPreferred em dados (compatibilidade ACL private v34.0.4),
    BucketOwnerEnforced em backups, quatro public access blocks e Deny sem TLS.
    Nenhuma regra lifecycle de objetos/expiração. Destroy operacional só runtime.
    Versionamento tem custo acumulado; nenhuma rotina automática de limpeza.
33. Role EC2 somente dados: GetBucketLocation/ListBucket no bucket; Get/Put/DeleteObject,
    AbortMultipartUpload/ListMultipartUploadParts nos objetos. ListBucket permite
    HeadBucket usado pelo SDK/probe; sem prefixo pois Nextcloud usa IDs próprios.
    PutObjectAcl permitido somente com s3:x-amz-acl=private porque v34.0.4 envia
    essa ACL em PUT/copy. Sem DeleteObjectVersion, CreateBucket, ACL pública,
    backups ou state. Containers que
    alcançam IMDS compartilham identidade da EC2: isso não isola processos no host.
34. Bootstrap compõe ARN apenas de backups, independente de runtime. GitHub mantém
    leitura limitada a backups/. Política backup_restore separada prepara Get/Put,
    multipart e listagem desse prefixo, sem exclusão e sem attachment. A futura
    identidade de execução será vinculada na fase autorizada; não conceder essas
    permissões à aplicação. Nenhuma rotina, workflow ou operação de backup/DR.
35. Configuração fresh install: hook pre-installation instala objectstore antes de
    occ install. Hook before-starting valida/reaplica somente instalação compatível.
    Marcador registra bucket/região/opções fora de variáveis dinâmicas; guard recusa
    local existente e troca silenciosa de storage. Não há migração nem fallback local.
36. stable34 oferece default AWS credential provider (shared config desabilitado);
    omitir key/secret habilita credenciais temporárias IMDSv2 da role EC2. HTTPS e
    região explícitos, hop limit 2 existente para bridge. Sem Access Key/Secret Key.
    verify_bucket_exists=false evita tentativa de CreateBucket pelo driver; bucket
    pertence a Terraform. Probe usa HeadBucket autenticado, somente leitura.
37. Conteúdo fica em S3, metadados no PostgreSQL; volumes de banco/config/apps continuam
    locais e não sobrevivem à perda de EC2/EBS. Preservar S3 não prova recuperação;
    restore histórico de versões/metadata será tratado futuramente, sem promessa PITR.
38. Validação somente estática/local com fixtures. Sem AWS real/VM/containers; upload,
    IAM/IMDSv2/renovação, duas execuções idempotentes e restart continuam pendentes.
    Parar ao final da Fase 4; não iniciar Fase 5 ou fases de workflows/backup/DR.

Fontes: [Nextcloud Primary Storage](https://docs.nextcloud.com/server/latest/admin_manual/configuration_files/primary_storage.html),
[driver S3 stable34](https://github.com/nextcloud/server/blob/stable34/lib/private/Files/ObjectStore/S3ConnectionTrait.php),
[hooks da imagem 34](https://github.com/nextcloud/docker/blob/master/34/apache/entrypoint.sh).

Compatibilidade: [S3ObjectTrait v34.0.4](https://github.com/nextcloud/server/blob/v34.0.4/lib/private/Files/ObjectStore/S3ObjectTrait.php),
[AWS Object Ownership](https://docs.aws.amazon.com/AmazonS3/latest/userguide/about-object-ownership.html).

## Decisões da Fase 5

39. Observabilidade local na EC2, sem serviço AWS gerenciado ou backend S3 de logs.
    Terraform/IAM/SG/instance_type e Primary Object Storage da Fase 4 preservados.
    Dados Prometheus/Grafana/Loki/Alloy são descartáveis e não integrarão backup/DR.
40. Prometheus v3.15.0, Grafana 13.2.3, Loki 3.7.8, Alloy v1.20.1, Node Exporter
    v1.12.1, com digests multiarch oficiais; Linux amd64/arm64 verificados, não
    executados. Sem latest/updates/plugins externos automáticos. Aplicação inalterada.
41. Node Exporter necessário para host: collectors CPU/meminfo/filesystem/diskstats/
    loadavg/stat/time/uname, 30s. UID nobody, sem host PID/network/capabilities;
    /proc,/sys e / root readonly para métricas reais, com leitura ampla como risco.
    Não adicionar cAdvisor nesta etapa: benefício por-container não justifica mounts/
    overhead antes de medir a stack. Dashboard distingue serviço de estado Docker.
42. Blackbox integrado Alloy verifica status.php com Host configurado, installed/
    maintenance/needsDbUpgrade e HTTP200; TCP PG/Redis sem secrets. Evita exporter
    externo. Labels job normalizados antes de remote_write interno Prometheus;
    job default do exporter contém prefixo. SQL/PING real continuam probe da Fase 3.
43. Logs por arquivos JSON Docker readonly, attrs project/service, stream stdout/stderr;
    filtro de projeto e remoção filename reduzem exposição/cardinalidade. UIDroot/GID473
    Alloy para permissões logs/estado, cap_drop ALL/no-new-privileges/rootfs readonly.
    Sem Docker socket: ro no socket não bloqueia mutações via API. Leitura de arquivos
    internos do daemon requer validação pós-upgrade/rotação e não isola clientes no host.
44. Rede observability internal; Alloy também backend, nenhuma frontend de observação.
    A entrega original usava Grafana loopback para SSH. A revisão operacional
    abaixo substitui esse acesso por IPv4 TCP/3000 com SG administrativo. Sem IAM/
    credenciais AWS novos; validar bloqueio de IMDS/egress na VM. Não alegar isolamento
    entre processos privilegiados com o mesmo host/profile.
45. Prometheus 48h/1GB de blocos; Loki 24h, compactor10m/delete-delay1h, ingestão32KiB/s/
    burst128KiB, caches32/16MB, replay128MB e baixa concorrência. Dockerjson5m/3 por
    container (~120MiB nominais atuais). Alloy cursors/WAL local com keepalive5–30m.
    Tempo/ingestão não são quota total: head/WAL/índices/transientes podem encher12GiB.
    Limites Loki podem rejeitar/perder logs de laboratório; não prometem durabilidade.
46. Grafana datasources e dashboard de11painéis provisionados; credenciais externas,
    quinto secret protegido/checksum, sem password real/env. Sem anonymous/signup/
    analytics/plugins automáticos. Admin env é inicialização, não rotação de SQLite.
47. Quatro regras Prometheus, incluindo indisponibilidade/ausência Nextcloud por2m;
    alertas visíveis em ALERTS no dashboard, sem Alertmanager ou notificação externa.
    Engine Grafana desabilitado por não ter regras próprias. promtool testa outage,
    pending, saudável, ausência de telemetria e recuperação, sem subir serviços.
48. Arquivos de monitoring têm fonte única; Ansible instala requests, copia com modos
    explícitos/renderiza Host e reinicia somente observabilidade cujos arquivos mudaram.
    Docker Compose não detecta conteúdo de bind paths iguais. Rerun inalterado não
    reinicia. Readiness Loki/Alloy via uri por bridge, pois imagens não têm HTTP client.
49. t3.medium/4 GiB e EBS12GiB mantidos, sem afirmar suficiência. Sem limites arbitrários
    de CPU/RAM. Medir toda stack/upload/carga de logs/restart/retention antes de sizing.
50. Validação local com Promtool/Loki/Alloy Windows oficiais/checksummed, Compose config,
    YAML/JSON/Jinja, PHP e Terraform mock. Não houve AWS/VM/daemon, upload, coleta ou
    render real. Syntax-check/lint Ansible dependem de Linux. Parar antes da Fase 6.

Fontes: [Node Exporter](https://github.com/prometheus/node_exporter),
[Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/),
[Loki retention](https://grafana.com/docs/loki/latest/operations/storage/retention/),
[Alloy Blackbox](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.blackbox/),
[Alloy files](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.file/),
[Docker JSON logging](https://docs.docker.com/engine/logging/drivers/json-file/).

## Fase 6 — decisões operacionais

51. Bucket dedicado de state passa da proposta persistent para bootstrap. Criação
    inicial usa state local em sessão SSO/federada; migração autorizada depois que
    bucket existir. CI não inicializa/aplica bootstrap e nunca usa backend local.
    Templates backend.tf.example ficam inativos no workspace; apenas CI autorizado
    gera backend.tf com bucket/key/região/account conferidos. Nenhuma migração agora.
52. State privado/versionado/SSE-S3/TLS/public-block/ownership enforced, sem expiração
    ou force_destroy. Três chaves conta/projeto/ambiente/root, workspace default,
    locking S3 use_lockfile=true, sem DynamoDB/KMS dedicada. CI só persistent/runtime
    e locks; SSO administra bootstrap. Mesma key runtime nos futuros deploy/destroy/DR.
53. Trust GitHub repo/environment exatos; owner/repo configuráveis e IDs opcionais
    para sub imutável. Com environment, branch precisa de proteção GitHub; guard
    DEPLOY_BRANCH adicional não substitui proteção de código/workflow. Repos privados
    suportados; conferir disponibilidade de proteções conforme plano do GitHub.
54. IAM GitHub sem AdministratorAccess, self-management/OIDC/bootstrap state ou
    attachment backup/restore. Gerencia dois buckets da aplicação por nome exato,
    EC2 regional/tagged e role/profile host exatos. Describe regional usa Resource *
    por APIs; dependências RunInstances têm escopo regional mais amplo. Ações
    Delete/Terminate só runtime para substituições de apply, sem workflow destroy.
55. Boundary persistente limita a role EC2 aos mesmos acessos S3 dados da Fase 4,
    ACL private condicionada. CreateRole exige boundary; não conceder remoção/
    alteração da boundary bootstrap nem AttachRolePolicy. PassRole exato apenas EC2.
    CI ainda é administrativo para aplicação/rede; proteger revisão/código/secrets.
56. workflow_dispatch default plan, apply exige APPLY antes de OIDC e cada apply.
    Conta/região/sizing/buckets visíveis/verificados, plan salvo aplicado no mesmo
    run sem artifacts; concurrency serializada sem cancelamento + locks S3. Sem
    rollback automático. Falha parcial conserva recursos e custos para reconciliação.
57. Runtime compõe ARN dados sem consultar bucket ainda inexistente para permitir
    primeiro plan conjunto. Apply persistent precede runtime; region/account comuns
    e guards S3 de instalação preservados. Sem migração de arquivos/storage local.
58. Default root EBS gp3 criptografado agora 30 GiB, parametrizado/limite até 30 GiB;
    tfvars/workflows não repetem default. t3.medium parametrizada permanece. Capacidade
    precisa de medição de imagens/volumes/PostgreSQL/WAL/monitoring/logs/temporários.
59. SSH runner via HTTPS IP discovery, TCP22 /32 temporário com tags/run/timestamp.
    Cleanup always por identidade exata; stale leases próprias >3h em próximo apply
    serializado. CIDRs administrativos preservados. Regra idêntica existente não
    muda de dono. Perda do runner pode exigir remoção administrativa; sem promessa
    absoluta. Timeout de 90 minutos/sessão de 2 horas/janela de 3 horas devem ser revisados juntos.
60. Host key Ed25519 de ssh-keyscan aceita só após comparar fingerprint console EC2
    autenticado do instance_id esperado. Sem TOFU/StrictHostKeyChecking=no. Falta/
    mismatch aborta; console não impresso. Chave privada 0600/diretório pai 0700 e cleanup
    local mesmo em erro AWS. Preflight confirma par SSH antes de provisionar.
61. Reutilizar deploy.yml Ansible e probes existentes, sem duplicação shell. Venv/
    collection no runner; syntax-check/lint safety antes de apply, real play só apply.
    Secrets só env do passo/no_log, passwords contrato já existente/sem rotação implícita.
    Aprovação da fase não autoriza executar pipeline/apply ou criar recursos reais.

62. Actionlint/checks offline/mocks/regressões aprovados, sem Linux/daemon local.
    Syntax-check/lint reais, OIDC/IAM/state/SSH/Ansible/stack e consumo continuam
    pendentes. Nenhum workflow backup/destroy/DR/Azure; parar antes da Fase 7.

Fontes: [GitHub OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws),
[S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3),
[EC2 IAM](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html),
[SSH fingerprint EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/connection-prereqs-general.html).

## Revisão operacional: instalação inicial do Nextcloud

O instalador desta plataforma roda no hook before-starting em cada inicialização,
depois da sincronização oficial e antes do healthcheck. version.php não comprova
installed=true. O auto-install nativo é desativado apenas pela separação dos nomes
de env administrativos; manutenção/upgrade da imagem permanecem oficiais.
maintenance:install usa ArrayInput em memória e NullOutput, com secrets montados;
não passar senhas em argv. Guard e identidade S3 precedem a instalação. Banco,
bucket ou dados locais preexistentes incompatíveis bloqueiam o caminho inicial;
instalação concluída não é repetida. Falha parcial exige recuperação específica
sem limpeza automática. Testes, diagnóstico e limites em
[nextcloud-initial-installation.md](nextcloud-initial-installation.md). Grafana
permanece uma investigação separada, sem mudança de rede/portas neste patch.


## Revisão operacional: Grafana no navegador e destino dinâmico

Docker Engine 29.8.2 não concretizou a publicação quando Grafana estava ligado
somente à bridge internal. Reprodução isolada no mesmo daemon confirmou o
binding nulo nessa topologia e publicação funcional com uma segunda bridge
não interna. Manter observability internal e adicionar grafana_access somente
ao Grafana, sem IPv6, é a menor alteração que preserva comunicação e coleta.

Publicar explicitamente 0.0.0.0:3000:3000 e autorizar TCP/3000 exclusivamente
para HTTP_ALLOWED_CIDRS administrativos no SG. Rejeitar /0 e IPv6 tanto no
controlador quanto no Terraform. Bind em todas as interfaces IPv4 não é uma
proteção isolada: acesso externo depende do SG e login continua obrigatório.
O Grafana passa a ter egress pela nova bridge; não afirmar isolamento de IMDS.
Nenhuma permissão IAM é ampliada. HTTP sem TLS é uma limitação explícita.

Runtime outputs/API fornecem o destino atual; o inventário, GRAFANA_PUBLIC_HOST,
GF_SERVER_ROOT_URL e URLs finais acompanham uma nova EC2/IP. CIDRs de origem
administrativa permanecem entradas distintas. Não adicionar Elastic IP, DNS ou
serviço gerenciado. Não liberar TCP/3000 para o runner: seus probes acontecem
dentro da EC2. Teste externo exige cliente com origem autorizada, após aplicação.
Não aplicar ou publicar este patch sem autorização separada. Evidências,
testes, impacto e procedimento em [acesso Grafana](grafana-browser-access.md).


## Estado operacional e documentação de portfólio — 2026-10-08

Fase 6 concluída por deploy real automatizado: OIDC, state S3/locking, SSH,
Ansible, aplicação S3 e observabilidade validados. Na correção final somente
Grafana foi recriado e somente a regra TCP/3000 foi criada, sem perda de volumes.
Declarações de validação pendente nas entregas originais são históricas.
Resultado atual: [Fase 6](phase-6.md); evidências: [portfólio](portfolio-evidence.md).

Preparar material sanitizado não autoriza tornar o repositório público nem
publicar no LinkedIn. IDs reais e configurações privadas ficam fora da documentação
de apresentação. Não converter contagens/testes mock em promessa de produção,
SLA ou recuperação. Backup manual, Destroy seguro e DR continuam planejados;
Fase 7 não foi iniciada. Nenhuma configuração funcional muda nesta revisão editorial.

## Fase 7 — PR1: Contrato de Recuperação

Em 2026-10-09, somente o contrato foi autorizado: docs, schemas, fixtures sintéticas
e testes locais, sem alterações operacionais. O estado acima é o histórico da Fase 6.
Regras em [phase-7.md](phase-7.md): conjunto completo/íntegro não é restore comprovado;
evidência funcional separada vinculada ao manifesto exato; modalidade inicial dependente
do bucket de dados e VersionIds preservados, sem proteção contra perda desse bucket.
PostgreSQL/configuração/arquivos locais/inventário exigem uma janela quiescente única.
Schemas não comprovam hashes, disponibilidade AWS ou veracidade de checks declarados.
Envelopes age-v1 são o formato previsto; implementação/custódia exigem PRs posteriores.
Nenhum backup, Destroy ou DR foi implementado. PR2 depende de nova autorização.
