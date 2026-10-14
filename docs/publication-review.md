# Revisão para publicação — 14/10/2026

O objetivo desta entrega é apresentar o laboratório como portfólio, sem novas
operações de infraestrutura. As EC2 já haviam sido removidas. Dados e backups
continuam privados no S3; tornar o código público não altera suas permissões.

## Escopo revisado

| Superfície | Resultado |
| --- | --- |
| Histórico Git alcançável antes desta entrega | 59 commits, todas as referências locais atualizadas; Gitleaks sem detecções |
| Arquivos operacionais no histórico | Sem state, planos, tfvars reais, backend configs reais, `.env` real ou chaves privadas identificados pelos nomes revisados |
| GitHub Actions | 53 execuções: logs de 51 examinados em memória, duas sem jobs; scanner sem detecções de segredos |
| Privacidade dos logs | 34 execuções contêm identificador da conta AWS ou CIDR administrativo; logs mantidos somente no repositório privado |
| Artifacts, releases, issues/PRs e webhooks | Nenhum encontrado na consulta |
| Deployments | 36 registros examinados; sem URLs de ambiente e sem detecções de segredos |
| Novas execuções de Actions | Desativadas nas configurações do repositório para a entrega como portfólio |
| Evidências privadas locais | Diretórios de validação e relatório de passagem operacional excluídos da publicação |

Os relatórios detalhados ficam em diretório local ignorado pelo Git. Logs brutos
do GitHub foram processados em memória, sem copiá-los para documentação ou commits.
Conta AWS e CIDR não são senhas; sua presença foi tratada separadamente da
detecção de credenciais. O histórico de autoria Git é preservado.

## Situação da entrega

Esta edição é uma cópia limpa para portfólio, com um novo histórico Git iniciado
no snapshot revisado. O repositório operacional original permanece privado;
seus commits antigos, logs, secrets e environments não são copiados nem apagados.
Os números de execução citados nos relatórios são referências ao histórico
privado, sem promessa de logs públicos. Não houve reescrita destrutiva de histórico.
As configurações da cópia não permitem novas execuções de Actions.

A apresentação foi atualizada para registrar deploy, backup, restauração e
login/download efetivamente validados. Também registra que não existe aplicação
online, que houve intervenções administrativas, que o restore final terminou
com falha de confirmação do cleanup SSM e que não foi concluído um workflow
Destroy ou ciclo totalmente automático. Nenhuma correção de infraestrutura
faz parte desta entrega.

Um scanner sem detecções reduz o risco, mas não constitui garantia absoluta.
Não são publicados credenciais, state, planos, configurações reais de backend,
tfvars privados, dados pessoais da aplicação ou o relatório operacional de
passagem. Para detalhes funcionais, consulte a [matriz de evidências](portfolio-evidence.md).
