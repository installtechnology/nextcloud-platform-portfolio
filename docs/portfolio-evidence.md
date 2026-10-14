# Evidências e apresentação de portfólio

## Encerramento do laboratório

Após as validações abaixo, o operador solicitou a remoção das duas infraestruturas
descartáveis. Dois planos administrativos removeram 16 e 17 recursos. A API AWS
confirmou ambas as EC2 em terminated e ausência de seus discos root e VPCs.
Os quatro buckets de dados de origem/destino, backups e state permaneceram
acessíveis e versionados. Não há demonstração online após o encerramento.
O [registro de remoção](runtime-removal-terminal.md) documenta o resultado.
Isso não implementa o workflow Destroy nem resolve a mistura de recursos
persistentes e descartáveis do root restore.

## Resultado atual do core — 14/10/2026

O laboratório demonstrou recuperação funcional na EC2 recriada, após a remoção
seletiva do runtime com persistência preservada. O deploy de preparação passou;
o restore completou as sete etapas, recuperou banco/configurações e verificou
as 173 versões selecionadas. Login web, API autenticada e download HTTP 200
foram executados no destino; os 196 bytes baixados corresponderam ao SHA-256
registrado no mapa de objetos do backup. Nextcloud 34.0.4, PostgreSQL e Redis
estavam healthy, com manutenção desligada e lease released.
O endpoint status.php também respondeu HTTP 200 pela URL pública, a partir do
computador do operador. Login e teste de download autenticados ocorreram no host.

Houve intervenções administrativas: evidência da host key por SSM, preservação
da receipt antiga, atualização do código revisado e reconciliação de falhas
antes de dados. O workflow final de restore ficou em failure no cleanup SSM;
a AWS confirmou zero sessões ativas na conferência posterior. Não apresentar
o run como success nem esse ciclo como automação sem intervenção. Onze objetos
adicionais foram preservados; a recuperação foi lógica, sem limpeza do bucket.
Os flags formais restore_proven/destroy_authorized permaneceram false.
O [relatório de recriação](phase-7-reprovisioning.md) registra as referências de execução do histórico privado.

As seções seguintes preservam a evidência histórica anterior a esse ciclo.

## Proposta de narrativa

Problema: transformar uma instalação manual de Nextcloud em laboratório
reproduzível, com identidade federada, infraestrutura declarativa e diagnóstico.
Resultado: deploy real AWS/GitHub Actions, Ansible/Compose, S3 Primary Object
Storage e observabilidade na mesma EC2.

O diferencial é separar administração, persistência e runtime e resolver falhas
reais: IAM por recurso, checksums Windows/Linux, contexto pré-install, retomada
segura e publicação Grafana. Apresentar decisões/evidências, sem sugerir HA,
SLA ou prontidão de produção. O restore independente da Fase 7 está descrito
separadamente abaixo, sem alegar validação do ciclo completo com Destroy.

## Fase 7 — backup e restauração independente realizados

Em 2026-10-13, `restore.yml` concluiu com sucesso o restore de um backup v2/SSE-S3
específico em EC2/bucket de destino independentes. A origem permaneceu preservada.
IDs, endereços, nomes reais de buckets e journals não integram esta documentação.

| Verificação | Resultado e origem da evidência | Limite |
| --- | --- | --- |
| Workflow de restore | `success`, job de 2min26s; sete etapas concluídas | Não é RTO universal nem ensaio completo após Destroy |
| PostgreSQL e configurações | Recuperados pelo executor supervisionado | Não homologa todos os modos de falha |
| Objetos S3 | 173 cópias, versões exatas e bytes verificados no destino | Backup continua dependendo do bucket original e suas versões |
| Aplicação | Nextcloud 34.0.4, installed=true, HTTP 200; PostgreSQL/Redis/Nextcloud healthy | Healthcheck não substitui teste de usuário |
| Estado final | Fencing restaurado, lease liberado, origem preservada | Sem Destroy ou remoção de dados |
| Teste externo — operador | Acesso autenticado, arquivo listado e download concluído | Relato manual separado dos testes automatizados; hash do download não comparado |
| Captura fornecida | Listagem de arquivos no Nextcloud restaurado | Não versionada, contém dados pessoais; não publicar sem sanitização |

O journal/manifesto não foram promovidos a `restore_proven=true`. Não foi emitida
evidência estruturada com checks não realizados, como comparação do download e
segundo dispatch sem reinstalação. O [resultado do PR8](phase-7-pr8.md) descreve
também a limitação do bootstrap do lease ao reprovisionar um host vazio.
Esta atualização documental não executa AWS e não autoriza Destroy.

## Evidências existentes da Fase 6

| Afirmação | Evidência observada | Limite |
| --- | --- | --- |
| Deploy automatizado | Workflow completo com sucesso; Ansible failed=0/unreachable=0 | Laboratório, não SLA |
| Autenticação federada | OIDC/AssumeRoleWithWebIdentity para identidade esperada | Omitir ARN, conta e subject concreto |
| State e locking | Três roots remotos; locks S3 liberados; plans finais 0/0/0 | Não comprova recuperação da aplicação |
| Mudança controlada | Persistent 0/0/0; runtime somente regra Grafana | Resultado daquele momento |
| Preservação no rerun | Só Grafana recriado; seis volumes e outros sete containers preservados | Não é teste de DR |
| Nextcloud | installed=true; HTTP 200; PostgreSQL/Redis/S3 autenticados | HTTP verificado no host |
| Nextcloud externo — relato manual | Página, login, upload, download, navegação e utilização: OK | Relatado pelo operador; sem screenshots ou evidências técnicas adicionais |
| Grafana externo — relato manual | Acesso sem túnel, login, dashboard, Prometheus/Loki e métricas/logs: OK | Relatado pelo operador, separado dos probes GitHub Actions |
| Conteúdo S3 | Consulta readonly associou um upload ao objeto e conferiu tamanho | Não publicar nome/ID/arquivo pessoal |
| Observabilidade | Readiness; datasources OK; dashboard provisionado | API não comprova render no navegador |
| Autenticação Grafana automatizada | API privada 401 sem autenticação; binding IPv4 | Probe técnico no host, distinto do login manual externo |
| Cleanup | Ausência de ingress SSH temporário e locks depois do run | Perda do runner exige auditoria |
| Segurança do código | Scan de secrets, regressões, lint e contratos | Não é auditoria de todo o histórico Git |

Este material usa validações já realizadas, sem nova consulta AWS/deploy nesta
preparação editorial. Logs privados não são copiados para o Git. A [Fase 6](phase-6.md)
contém o resumo sanitizado para revisão.

## Acesso externo: relato do operador e prova técnica

O operador confirmou testes externos pelo navegador, a partir de acesso
administrativo autorizado. Registrar como **validação manual relatada pelo operador**:

| Serviço | Operação | Resultado relatado |
| --- | --- | --- |
| Nextcloud | Acesso à página | OK |
| Nextcloud | Login administrativo | OK |
| Nextcloud | Upload e download do arquivo | OK |
| Nextcloud | Navegação e utilização da aplicação | OK |
| Grafana | Acesso direto, sem túnel SSH | OK |
| Grafana | Login administrativo | OK |
| Grafana | Dashboard platform-overview | OK |
| Grafana | Conexões Prometheus e Loki | OK |
| Grafana | Visualização das métricas e logs | OK |

Todos os testes manuais relatados funcionaram. Não foram fornecidos screenshots
ou evidências técnicas adicionais dessas operações. O relato não é teste
automatizado do GitHub Actions nem evidência visual anexada. Endereço/CIDR,
credenciais e dados pessoais são omitidos.

Upload/download comprova apenas o funcionamento relatado pela interface, não
inspeção independente dos objetos S3. A consulta readonly de metadados/objeto e
tamanho já registrada é uma verificação técnica separada; não se infere essa
inspeção a partir do teste manual. Nenhuma dessas verificações comprova Destroy
ou Disaster Recovery por si só. As evidências do restore da Fase 7 estão
registradas na seção específica acima.

## Capturas para uma apresentação

**Não há screenshots versionados prontos para divulgação.** Não há galeria
fictícia nem nova captura nesta entrega. Para complementar depois:

1. Workflow concluído, com etapas OIDC/Terraform/Ansible/cleanup visíveis.
2. Dashboard Grafana com CPU/RAM/disco, disponibilidade e logs fictícios.
3. Nextcloud com pasta/arquivo demonstrativo, sem dados pessoais.
4. Trecho sanitizado de plan/recap, mostrando controle de mudanças e failed=0.
5. Diagrama do README, sem valores concretos da conta.

Ocultar definitivamente barra de endereços, IPs/CIDRs, conta, ARNs/IDs de
recursos/execuções, buckets reais, emails/usuários pessoais, chaves/fingerprints,
headers/tokens e arquivos privados. Usar recorte ou tarja opaca; desfocagem
reversível não basta. Não incluir telas de Secrets, env, tfvars, state, planos
binários ou credenciais IMDS. Revisar também nomes/metadados das imagens finais.

## LinkedIn

Sequência sugerida: objetivo → diagrama → OIDC/Terraform/Ansible/Compose →
evidências → decisão técnica aprendida → próximos passos. Usar “deploy validado
em laboratório”, não “pronto para produção” ou “recuperação comprovada”.
Não divulgar URL/credenciais do ambiente. Repositório privado pode ser apresentado
com material sanitizado; torná-lo público não é pré-requisito. Publicação no
LinkedIn, commit/push e mudança de visibilidade exigem autorizações próprias.

## Próximas entregas

Backup manual coerente de banco/config/metadata, Destroy somente runtime com
gate de backup e DR com recuperação de usuário/arquivo são planejados. Bucket
provisionado não significa backup existente. Medir RPO/RTO somente após testes
de restauração; não inferi-los do tempo de provisionamento ou versionamento S3.
