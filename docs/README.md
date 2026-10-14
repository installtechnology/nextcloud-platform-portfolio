# Documentação Nextcloud Platform

Decisão vigente do PR7: [SSE-S3, sem dependência de age](phase-7-pr7-sse-s3.md).
Novos backups usam contrato v2; restauração autenticada por IAM, sem fornecimento
de chave de descriptografia. Backup real e restauração em destino independente
foram executados; o [resultado atual do PR8](phase-7-pr8.md#resultado-real-e-limites-da-evidência)
separa verificações automatizadas, relato manual e limites da comprovação.

Estado atual: **laboratório encerrado, com deploy, backup e recuperação funcional demonstrados**.
As duas EC2 e suas redes foram removidas por planos administrativos; dados,
backups e state foram preservados. Não há aplicação online. A Fase 6 concluiu
um deploy automatizado bem-sucedido na AWS.
Fase 7: [PR1 — Contrato de Recuperação](phase-7.md) e
[PR2 — Validador Offline](phase-7-pr2.md),
[PR3 — Captura Local](phase-7-pr3.md) e
[PR4 — IAM preparatória](phase-7-pr4.md) publicados, sem homologação operacional.
[PR5 — Criptografia e transporte](phase-7-pr5.md) publicado como preparação
simulada e [PR6 — Orquestração manual bloqueada](phase-7-pr6.md) publicado,
sem homologação operacional ou operações no ambiente.
Os estados acima descrevem as entregas preparatórias originais. PR7 realizou o
backup real; PR8 concluiu um restore automatizado independente, com download
relatado pelo operador. Em 14/10/2026, o core foi recuperado também na EC2 recriada,
com login web e download/hash confirmados. O [resultado da recriação](phase-7-reprovisioning.md)
registra as intervenções e a falha de cleanup SSM após restore concluído.
Destroy seguro e automação sem intervenções permanecem pendentes.
O root restore mistura persistência e runtime; o encerramento usou remoção
seletiva, sem modificar essa arquitetura.

| Leitura | Finalidade |
| --- | --- |
| [README principal](../README.md) | Objetivo, arquitetura, stack, fluxo e roadmap |
| [Fase 6](phase-6.md) | Resultado operacional sanitizado, testes e limitações |
| [Fase 7 / PR1](phase-7.md) | Contrato de consistência e garantias; schemas e testes locais |
| [Fase 7 / PR2](phase-7-pr2.md) | Validador offline, cobertura e limites de comprovação |
| [Fase 7 / PR3](phase-7-pr3.md) | Captura local, journal, isolamento e ensaios pendentes |
| [Fase 7 / PR4](phase-7-pr4.md) | IAM de backup opt-in, limites de permissões e validação offline |
| [Fase 7 / PR5](phase-7-pr5.md) | Criptografia age e transporte simulados, inventário pareado e limites de completion |
| [Fase 7 / PR6](phase-7-pr6.md) | Workflow bloqueado, conectividade pendente, preflight sintético e proposta POSIX isolada |
| [Fase 7 / PR8 — resultado real](phase-7-pr8.md) | Restore independente executado, validação manual e limites; preparação e histórico técnico |
| [Recriação e recuperação do core](phase-7-reprovisioning.md) | Resultado de 14/10: preparação, sete etapas, login/download/hash, intervenções e cleanup SSM |
| [Homologação Linux — histórico](phase-7-linux-qualification.md) | Preparação reproduzível; testes POSIX e qualificação PR7 executados posteriormente; ensaios de falhas ainda limitados |
| [Desenho da fixture S3/IMDS](phase-7-s3-imds-fixture-design.md) | Sandbox preparatório separado, sem homologação por execução de containers |
| [Evidências de portfólio](portfolio-evidence.md) | O que foi comprovado e como preparar capturas |
| [Revisão para publicação](publication-review.md) | Arquivos, histórico e superfícies do GitHub revisados antes da exposição pública |
| [Decisões](decisions.md) | Racional técnico e revisões arquiteturais |
| [Terraform State](terraform-state.md) | Backend S3, locking e bootstrap para novos ambientes |
| [Remoção pelo terminal](runtime-removal-terminal.md) | Gerar, conferir e aplicar planos seletivos preservando dados e backups |
| [GitHub Actions](../.github/workflows/README.md) | Variables/Secrets por nome e gates |
| [Ansible](../ansible/README.md) | Roles, pré-requisitos, inventário e SSH |
| [Docker](../docker/README.md) | Stack, imagens, arquivos protegidos e storage |
| [Monitoring](../monitoring/README.md) | Coleta, retenção, provisionamento e riscos |
| [Instalação Nextcloud](nextcloud-initial-installation.md) | Guard, instalador e retomada |
| [Acesso Grafana](grafana-browser-access.md) | Diagnóstico de rede e correção aplicada |
| [IAM EC2](iam-runtime-authorization.md) | Auditoria por recurso e tags |

Os relatórios [Fase 1](phase-1.md), [2](phase-2.md), [3](phase-3.md),
[4](phase-4.md), [5](phase-5.md) e [recuperação Ansible](ansible-application-recovery.md)
preservam o histórico. “Não executado nesta entrega” descreve o momento original,
não o estado final. Identificadores reais foram substituídos por placeholders.

Dados operacionais ficam em configurações/relatórios privados ignorados pelo Git.
A revisão para publicação tem escopo próprio documentado; resultados de scanners
não representam garantia absoluta de ausência de informações sensíveis.
