# Fase 2 — Ansible + Docker — 2026-10-07

Implementados bootstrap Ubuntu e instalação/configuração Docker. Nenhuma VM
configurada, recurso AWS criado, terraform apply/destroy ou backend migrado.
Nextcloud/PostgreSQL/Redis/Prometheus/Grafana/Loki/Alloy não foram instalados.
Fase 3 não iniciada.

## Ajuste autorizado da EC2

Default instance_type alterado para t3.medium exclusivamente no root runtime/
variables.tf. Recurso continua usando var.instance_type. Exemplos omitem o
valor para herdar default e testes comparam com a variável. Referências ao
default anterior foram atualizadas. Root bootstrap/persistent não alterado.
Nenhuma VM real aumentada; custo futuro de EC2 será maior que o default anterior
e deve ser cotado na região do cliente antes do apply. Sizing final da stack
continua dependendo de medição dos sete componentes e dos picos de backup/restore.

## Arquivos novos e árvore Ansible

```text
ansible/
├── .ansible-lint                  [novo]
├── ansible.cfg                    [novo]
├── README.md                      [novo]
├── requirements-control.txt       [novo]
├── inventory/
│   ├── README.md                  [alterado]
│   └── hosts.yml.example          [novo]
├── playbooks/
│   ├── README.md                  [alterado]
│   └── bootstrap.yml              [novo]
├── roles/
│   ├── README.md                  [alterado]
│   ├── common/
│   │   ├── defaults/main.yml      [novo]
│   │   └── tasks/main.yml         [novo]
│   └── docker/
│       ├── defaults/main.yml      [novo]
│       └── tasks/
│           ├── main.yml           [novo]
│           └── validate.yml       [novo]
└── tests/
    ├── check_static.py            [novo]
    └── requirements-static.txt    [novo]
```

Outros alterados: terraform/aws/runtime/variables.tf,
terraform/aws/runtime/tests/base.tftest.hcl,
environments/lab/runtime.tfvars.example,
environments/customer-template/runtime.tfvars.example,
README.md, docs/decisions.md, docs/phase-1.md e .gitignore.
AGENTS.md foi lido e não precisou mudar. Este relatório é novo.

## Comportamento e limites

common atualiza cache APT, instala ca-certificates/curl/python3-apt/python3-debian,
confirma usuário existente, cria grupo e diretórios root:grupo 0750. Não configura
senha/sudo/chave nem cria usuário automaticamente. Raiz /opt/nextcloud-platform
com docker/, monitoring/, data/ e backups/, sem mounts/persistência/backup.

docker instala docker-ce/docker-ce-cli/containerd.io/docker-buildx-plugin/
docker-compose-plugin via repositório oficial HTTPS, chave restrita e deb822;
aborta em pacotes conflitantes, habilita/inicia Docker via systemd e valida
daemon/Compose. Grupo docker é opt-in false, pois confere controle de root.
Command é usado somente em probes docker info/compose version, sem changed;
nenhum shell, script remoto, hello-world, compose up ou imagem da stack.

Inventário obtém IP de PLATFORM_HOST e usuário de PLATFORM_SSH_USER. Runner
futuro exportará public_ip do mesmo state runtime. SSH via ssh-agent/--private-key,
SG restrito e known_hosts verificado; OIDC não autentica SSH. Chaves privadas
nunca entram no repo. Operação futura detalhada em ../ansible/README.md.

Idempotência projetada por módulos state=present/directory/started, append=true,
cache válido e queries changed_when=false; não force restart ou latest.
Não foi comprovada em VM: duas execuções consecutivas com segunda changed=0
continuam sendo a evidência pendente. Primeiro --check em host limpo é parcial.

## Comandos executados e evidências

- Leitura de AGENTS.md e inventário de referências com rg.
- Get-Command para Python, Ansible, ansible-lint, WSL e Docker.
- wsl --list --quiet, wsl --list --verbose e wsl --status: WSL instalado, sem distro.
- Python: inspeção de disponibilidade dos parsers; PyYAML/Jinja não preinstalados.
- python -m pip install --target .validation-libs PyYAML==6.0.3 Jinja2==3.1.6:
  dependências de validação isoladas no workspace/ignoradas pelo Git.
- Com PYTHONPATH=.validation-libs, python ansible/tests/check_static.py:
  YAML/Jinja de 8 arquivos, 27 tasks de roles e contratos estáticos passaram.
- terraform fmt -check -recursive: passou, incluindo testes.
- terraform validate -no-color nos roots bootstrap/persistent/runtime:
  configuração válida nos três (provider local autorizado).
- terraform test -no-color no runtime: 2 testes simulados passaram, 0 falharam.
- ansible-playbook -i inventory/hosts.yml.example playbooks/bootstrap.yml
  --syntax-check: tentativa não executou a validação, pois binário não existe.
- rg confirmou: default executável definido só em variables.tf; nenhum exemplo
  fixa o novo tipo e nenhuma referência ao default anterior permanece.

**Limitação de validação:** syntax-check oficial e ansible-lint não puderam rodar
neste Windows, que não tem Ansible/controlador Linux/WSL distro/Docker disponíveis.
Não instalar OS, VM ou AWS para contornar a limitação. O teste estático valida
parsing YAML/Jinja, imports/roles, módulos/escopo e contratos; não é o parser
Ansible, não consulta argumentos efetivos de todos os módulos nem verifica SSH,
sudo, APT, Docker ou idempotência em Ubuntu. Ver
[restrição oficial de controlador Windows](https://docs.ansible.com/projects/ansible/latest/os_guide/intro_windows.html).

Molecule não adicionado: sem Docker/VM local e sem necessidade de ampliar a
complexidade. requirements-control.txt e instruções Linux permitem executar
syntax-check/lint posteriormente, sem conexão a host durante esses checks.

## Riscos e pendências

Antes da execução real: controlador Linux com dependências, syntax-check/lint,
Ubuntu limpo acessível, chave privada externa, host key verificada, sudo e egress
APT/HTTPS. Não há VM autorizada/disponível nesta entrega.

Versões Docker não congeladas: state=present é idempotente numa VM existente,
mas nova VM instala stable disponível naquele momento. Pinning/controlar upgrades
é melhoria futura. Permissões específicas de volumes/UIDs/secrets da aplicação
serão definidas na Fase 3 ou fases de storage; diretórios atuais estão vazios.
Em future containers, revisar logs/disco/portas e medir sizing antes de avançar.
Grupo Docker só deve ser concedido conscientemente; false não revoga membros
existentes. Nenhum backup automático ou serviço da aplicação implementado.

Decisões registradas em decisions.md. Fase 2 entregue com limites de validação
explícitos; aguardar instrução antes da Fase 3.
