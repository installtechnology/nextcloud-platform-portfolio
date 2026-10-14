# Fase 6 — deploy automatizado validado na AWS

**Concluída operacionalmente em 8 de outubro de 2026.** Relatório sanitizado:
omite conta, IPs, ARNs concretos, IDs de recursos/execuções, nomes reais de buckets,
fingerprints e valores de state. Evidências privadas permanecem fora do Git.

## Escopo e resultado

GitHub Actions executou um deploy real via OIDC, Terraform remoto, SSH verificado,
Ansible e Docker Compose. Bootstrap e migração de seu state precederam o workflow.
Há três roots/states independentes; CI opera somente persistent/runtime. Disparo
manual, padrão plan; apply exige confirmation=APPLY e Environment/branch autorizados.

| Verificação | Resultado observado |
| --- | --- |
| Disparo final | Uma execução da correção Grafana, código publicado na branch autorizada |
| Preflight | Seis Secrets presentes; par SSH conferido sem mostrar valores |
| AWS | OIDC/AssumeRoleWithWebIdentity e conta/região esperadas confirmados |
| Backend | S3 por root com use_lockfile=true; locks liberados |
| Persistent apply | 0 added / 0 changed / 0 destroyed |
| Runtime apply | 1 added / 0 changed / 0 destroyed; somente TCP/3000 administrativo |
| Plans posteriores | Persistent/runtime: 0 add / 0 change / 0 destroy |
| State final | Bootstrap: 11 recursos; persistent: 12; runtime: 16 |
| Preservação | Mesma EC2/EBS; IAM/buckets/VPC/subnet sem alteração; seis volumes mantidos |
| Containers | Oito running; somente Grafana recriado na correção final |
| Ansible | ok=61, changed=3, unreachable=0, failed=0, skipped=6 |
| Encerramento | Workflow completo com sucesso; SSH temporário removido |

Nextcloud, PostgreSQL, Redis, Prometheus, Grafana e Node Exporter healthy.
Loki e Alloy running com readiness HTTP 200, sem healthcheck Docker configurado.
Nextcloud: installed=true, maintenance=false e needsDbUpgrade=false. Integração
PostgreSQL/Redis/S3 autenticada OK. Apache respondeu HTTP 200 para status/login
no host; nenhuma reinstalação foi necessária na correção Grafana.

Grafana: TCP/3000 no host, database=ok; health dos datasources Prometheus/Loki
e UID do dashboard confirmados. API privada 401 sem autenticação; anônimo/signup
desabilitados. Destino/URL raiz acompanham IP descoberto via outputs/API.

**Limite dos probes:** testes HTTP finais dentro da EC2 não equivalem ao acesso
externo pelo navegador. Nenhuma regra TCP/3000 para o runner foi adicionada.

## Validação manual relatada pelo operador

Testes externos no navegador, a partir do acesso administrativo autorizado:

| Serviço | Operações relatadas | Resultado |
| --- | --- | --- |
| Nextcloud | Página, login administrativo, upload, download, navegação e utilização | Todas OK |
| Grafana | Acesso direto sem túnel, login administrativo, dashboard platform-overview, conexões Prometheus/Loki, métricas e logs | Todas OK |

Este é um relato manual do operador, separado dos testes automatizados do
GitHub Actions. Não foram fornecidos screenshots ou evidências técnicas adicionais.
Upload/download não é prova independente de inspeção direta S3. A consulta
readonly S3 previamente registrada é uma verificação separada, não uma inferência
do teste de navegador. Omitir endereços, identificadores e credenciais do relato.

## Problemas resolvidos e decisões

- IAM: separar RequestTag de recursos novos e ResourceTag de dependências,
  sem AdministratorAccess ou ec2:*. [Auditoria EC2](iam-runtime-authorization.md).
- Lockfiles: hashes oficiais Windows/Linux, mantendo provider AWS 6.67.0.
- Guard: carregar apps.config.php com contexto compatível, sem ignorar configs.
- Instalador: preparação S3 antes da instalação, um único instalador no hook;
  installed=true não reinstala. Estado parcial bloqueia sem limpar banco/volumes.
  [Fluxo e testes](nextcloud-initial-installation.md).
- Grafana: reproduzir binding nulo na bridge internal; manter a rede e adicionar
  bridge dedicada para publicação IPv4. [Correção aplicada](grafana-browser-access.md).

## Segurança e validação

OIDC/role/state bucket sobrevivem ao futuro Destroy runtime. Permissions boundary
limita EC2 ao bucket de dados; state/backups não são acessíveis pela aplicação.
CI verifica identidade, região, branch, Environment e par SSH. Secrets ficam
fora de argumentos/logs/artifacts. Host key é verificada por canal AWS autenticado.
Cleanup remove somente SSH /32 da execução; sua ausência foi conferida na AWS.
Always não é garantia absoluta diante de perda do runner.

HTTP/80 e Grafana/3000 restritos; banco/cache/collectors sem portas externas.
Não há anônimo Grafana, IPv6 publicado, Docker socket de coleta, backup automático
ou destroy após falha. HTTP sem TLS continua limitação do laboratório.

Antes da publicação final: fmt/validate/plans; 5 testes Terraform mock runtime;
13 CI; 17 storage guard; 18 instalador; Compose config; estáticos Docker/Ansible/
monitoring/CI/IAM; PHP lint; syntax-check/ansible-lint; scan de secrets e diff-check.
Prometheus/Loki/Alloy e actionlint passaram na revisão da correção. Testes de
instalação usam fixtures/adapters, sem banco/volumes operacionais.

Amostra sem carga: RAM 1,3/3,7 GiB usada; disco 35% utilizado, cerca de 19 GiB
livres; oito imagens/containers e seis volumes. Não é benchmark ou sizing de
produção. EC2/EBS parametrizados; laboratório: t3.medium e gp3 criptografado 30 GiB.
Warnings não bloqueantes: parsing do inventário de exemplo no syntax-check e
ajuste PATH do lint. Deploy real usou inventário JSON dinâmico, sem falhas.

## Pendências reais

Backup manual completo, Destroy condicionado a backup e DR por conjunto exato
ou mais recente ainda não foram implementados/testados. Preservar S3 não recupera
PostgreSQL/configuração. TLS, carga representativa e exercício de recuperação
permanecem pendentes. A validação externa foi registrada como relato manual,
sem capturas ou evidências adicionais. Não afirmar HA, SLA ou RPO/RTO.
Consulte [evidências](portfolio-evidence.md), [state](terraform-state.md) e
[workflow](../.github/workflows/README.md). Relatórios anteriores são históricos.
