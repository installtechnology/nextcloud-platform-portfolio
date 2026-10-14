> Registro histórico. A separação de buckets/IAM e a configuração de storage foram revisadas na [Fase 4](phase-4.md); seguir a documentação atual para instalação nova.

# Fase 3 — Nextcloud + PostgreSQL + Redis — 2026-10-07

Configuração/automação da stack implementada, **não executada em VM**. Não houve
terraform apply/destroy, infraestrutura AWS, S3 Primary Object Storage,
observabilidade, workflows, backup, restore ou DR. Default EC2 preservado.
AGENTS.md foi lido; nenhuma regra permanente nova necessária.

## Árvore relevante e arquivos criados

```text
docker/
├── compose.yml                        [novo]
├── .env.example                       [novo]
├── README.md                          [alterado]
├── postgres-init.sh                   [novo]
├── redis-entrypoint.sh                [novo]
├── configure-nextcloud.sh             [novo]
├── platform.config.php                [novo]
├── nextcloud-healthcheck.php          [novo]
├── nextcloud-integration.php          [novo]
└── tests/check_static.py              [novo]
ansible/
├── requirements.yml                  [novo]
├── README.md                         [alterado]
├── playbooks/
│   ├── bootstrap.yml                  [preservado: somente Fase 2]
│   ├── deploy.yml                     [novo]
│   └── README.md                      [alterado]
├── roles/
│   ├── README.md                      [alterado]
│   ├── common/                        [preservado]
│   ├── docker/                        [preservado]
│   └── nextcloud_stack/
│       ├── defaults/main.yml          [novo]
│       ├── tasks/main.yml             [novo]
│       ├── tasks/validate.yml         [novo]
│       └── templates/env.j2           [novo]
└── tests/check_static.py              [alterado]
docs/
├── decisions.md                      [alterado]
└── phase-3.md                        [novo]
.gitattributes                       [novo: LF]
.gitignore                           [alterado: tools/secrets]
README.md                            [alterado]
```

Terraform, environments, requirements-control.txt, inventário SSH e roles
common/docker não tiveram configuração alterada nesta fase. .env real não foi
criado no repo; somente o template Ansible produzirá esse arquivo no host futuro.
.validation-tools/.validation-libs são ferramentas locais ignoradas, não entregáveis.

## Versões e decisões

| Componente | Versão/imagem |
| --- | --- |
| Nextcloud | 34.0.4-apache |
| PostgreSQL | 17.11-bookworm |
| Redis | 7.4.8-bookworm |
| Collection Ansible | community.docker 5.3.0 |

As três imagens têm digest SHA-256 multiarch fixado no Compose; tags verificadas
via manifests públicos com HTTP 200, amd64/arm64 disponíveis. Isso não comprova
execução. Imagens escolhidas a partir das fontes oficiais mantidas. Sem latest.
Digests completos em compose.yml; não repetir imagens em defaults/templates.

Arquitetura: HTTP Nextcloud, rede backend internal para DB/Redis sem ports,
frontend só Nextcloud. Named volumes Nextcloud/PostgreSQL locais, perdidos com
EC2/EBS. Redis autenticado e sem persistência crítica. Nenhum CPU/RAM limit novo.
Nextcloud usa APCu local/Redis locking+distributed e PostgreSQL não superuser.
Credencial administrativa PostgreSQL separada, nunca montada no Nextcloud.

Auto instalação oficial só no primeiro bootstrap; hook complementar não impede
cópia dos configs oficiais. Compose com healthy dependencies e probes; Ansible
pull missing/up/wait via módulos, occ/probe readonly. Senhas externas, paths em
.env, secret dir 0700 e arquivos montados 0444 para processos autorizados.
Passwords divergentes abortam; nomes/project/volumes devem permanecer estáveis.
Detalhes de segurança e pré-requisitos em ../docker/README.md e ../ansible/README.md.

## Comandos executados e resultados

- Leitura do texto anexado, AGENTS.md, arquivos existentes e pesquisa rg.
- Consulta às fontes oficiais de imagens, entrypoints, configurações e APIs OCP.
- Consulta anônima ao Docker Registry dos três manifests: HTTP 200 e digests.
- Get-Command: Docker Engine/Ansible/ansible-lint/PHP não instalados globalmente.
- Download isolado Docker CLI 29.5.2 e Compose Plugin v5.6.0 (digest do asset
  verificado); somente docker.exe/plugin, **não daemon/serviço**.
- Download PHP CLI 8.5.11 oficial isolado, checksum SHA-256 conferido.
- Com PYTHONPATH=.validation-libs, python ansible/tests/check_static.py:
  **13 YAML/Jinja, 43 tasks e contratos passaram**, preservando checks Fase 2.
- python docker/tests/check_static.py --docker-cli .validation-tools/docker.exe
  --docker-config .validation-tools/config: **passou**, incluindo render de
  env.j2 com StrictUndefined, contratos da stack e docker compose config --quiet
  real com dados fictícios/arquivo temporário. POSTGRES_DB/admin user vazios
  foram rejeitados como esperado. Nenhum segredo real/.env foi produzido.
- Git Bash -n em postgres-init.sh, redis-entrypoint.sh e configure-nextcloud.sh:
  **passou** após permitir comunicação local do runtime Git Bash (sandbox bloqueava).
- PHP CLI -n -l em platform.config.php, nextcloud-healthcheck.php e
  nextcloud-integration.php: **passou**. Leitura isolada do config confirmou
  strings APCu/Redis corretas com domínio fictício; sem inicializar Nextcloud.
- terraform fmt -check -recursive: **passou**.
- terraform validate -no-color em bootstrap/persistent/runtime: **válidos os três**.

Ferramentas portáteis apenas viabilizam lint/config neste Windows. Não há engine,
containers, volumes da aplicação, serviço ou VM Linux local. Syntax-check/lint
Ansible continuam pendentes de controlador Linux com requirements-control.txt e
requirements.yml. O teste estático não substitui engine Ansible ou execução real.
No futuro, executar syntax-check para bootstrap/deploy e lint antes do deploy.

## Teste operacional futuro (nenhuma etapa abaixo executada)

1. VM Ubuntu limpa, SG/SSH/known_hosts/sudo e secrets externos prontos.
2. Ansible deploy.yml prepara host, obtém imagens e sobe stack.
3. PostgreSQL/Redis healthy; Nextcloud instalado/healthy pelo auto bootstrap.
4. Probes confirmam DB SELECT 1, Redis AUTH/PING/cache e HTTP status.
5. Acesso http://IP, login, criação de usuário e upload local fictício.
6. Restart sem remoção de volumes: banco/config/usuário/arquivo preservados.
7. Rerun Ansible com mesmos secrets/nomes/project/volumes; sem reinstalação,
   credenciais/dados preservados e recap de segunda execução sem mudanças indevidas.
8. Registrar versões, healthchecks/recaps e consumo CPU/RAM/disco/concurrency.

## Riscos e pendências

HTTP sem TLS somente para demo temporária e dados fictícios. Root/operador Docker
pode ler secrets; Compose local não é cofre criptografado. Digests não se atualizam
automaticamente; testar patches/upgrades explicitamente. Inicialização parcial
falhada de DB/app exige investigação, sem apagar volumes ou reinstalar à força.

Disco gp3 12 GiB pode limitar imagens/uploads/logs; nenhum aumento implementado.
4 GiB/t3.medium não certificam a stack completa/observabilidade. Medir antes de
alterar sizing. Sem cron dedicado; background jobs da instalação devem ser
avaliados antes de uso contínuo. Lint/syntax-check Ansible e todos os testes
funcionais/idempotência permanecem pendentes em Linux.

Volumes não sobrevivem ao destroy. Fase 4 deverá definir migração explícita ou
ambiente novo aprovado antes de ligar S3 sobre dados locais; nada é convertido
automaticamente. Backup/DR só nas fases autorizadas. Fase 4 não iniciada; parar
e aguardar nova instrução.
