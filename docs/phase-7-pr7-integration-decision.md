# PR7 — decisão bloqueante da integração operacional

> Estado atualizado: a integração local está implementada e descrita em
> [PR7 integrado](phase-7-pr7-integrated.md). As lacunas e etapas futuras abaixo
> registram a evolução preparatória; não implicam ativação ou homologação na AWS.

Registro histórico anterior à aprovação da mudança de ownership. A decisão foi
aprovada e implementada localmente em
[ownership administrativo](phase-7-pr7-policy-migration.md). As afirmações abaixo
sobre Terraform/IAM inalterados e ausência da autoridade descrevem aquele
checkpoint, não o código atual. A ativação operacional continua pendente.

Estado local, sem publicação ou operações AWS. Esta análise não autoriza IAM,
SSM remoto, alteração de policy, deploy, manutenção, backup ou restauração.
O PR7 não está integrado nem pronto para execução operacional.

## Incompatibilidade comprovada no código

`terraform/aws/bootstrap/operational-iam.tf`, statement
`ManageTwoApplicationBucketsConfiguration`, autoriza a role de deploy a executar
PutBucketPolicy nos buckets data/backups. `terraform/aws/persistent/main.tf`
gerencia a policy de dados como recurso Terraform. Não existe uma autoridade
administrativa exclusiva implementada; `PolicyAuthority` rejeita execução.

O lock/journal PR3 está no host alvo. Ele não impede chamadas PutBucketPolicy
feitas por outro cliente que não adquire esse lock. Comparar hashes antes/depois
detecta conflitos, mas não evita sobrescrever uma mudança concorrente entre
leitura e escrita. A API não expõe If-Match/CAS. O Deny temporário de política só
passa a restringir editores depois de aplicado; não protege a primeira alteração.
[API oficial](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketPolicy.html).

Logo, não implementar PolicyAuthority como retorno verdadeiro, flag de Environment,
arquivo de confirmação ou dois inventários iguais. Isso não concluiria o controle
exclusivo exigido. Também não retirar permissões da role de deploy implicitamente:
essa mudança afetaria o fluxo Terraform persistent hoje existente.

## Decisão necessária antes da parte dependente

Recomendação: uma única autoridade administrativa para as alterações da política
de dados, com entrypoint serializado e recuperação explícita. O desenho deve
restringir os caminhos alternativos de alteração e adaptar o deploy/persistent
sem delegar PutBucketPolicy à role de backup ou ampliar a role da aplicação.
O bootstrap administrativo inicial e a recuperação emergencial precisam de
procedimentos distintos revisados, sem bypass do guard no deploy ordinário.
Essa é uma alteração de arquitetura/autorização do deploy existente e exige
aprovação antes de implementar seu patch IAM/Terraform/workflow.

Root e administradores capazes de modificar os próprios controles não podem
ser tratados como escritores não confiáveis que o lock local consegue impedir.
A exceção do bucket owner root é documentada pela AWS; restringi-la exige controles
externos, como SCP/endpoint. Não introduzir Organizations/SCP nesta etapa.
Definir explicitamente a fronteira de administradores confiáveis e o procedimento
break-glass; não alegar proteção contra administração hostil.

Alternativa: freeze administrativo coordenado pelo operador, restringindo o uso
a uma janela supervisionada. Esse controle humano não equivale à exclusão técnica
automática solicitada. Só pode ser adotado com mudança explícita do critério de
aceite, sem escondê-lo atrás de uma flag ou evidência sintética. Não recomendado
para declarar o workflow completo conforme os requisitos atuais.

## Correção independente realizada

`scripts/backup/operational/inventory_hooks.py` agora preserva os inventários
antes/depois como par. O engine PR3 substitui inventory_observation após o hook
final; antes desta correção, o adapter operacional perdia o primeiro recibo no
journal. Contratos, engine PR3 e hooks simulados não foram alterados.

O adapter conserva uma cópia privada do primeiro recibo; antes do segundo exige
igualdade com a observação durable do journal e com captura, consistency_id e
fence. Também rejeita reutilização e chamada after sem before. Ambos os recibos
mantêm backup_complete=false; nenhum marker operacional foi habilitado.

Testes em `scripts/backup/operational/tests/test_inventory_hooks.py` verificam o
par retornado, recibo adulterado, consistência divergente, ausência do primeiro
inventário e ausência de alias mutável. São testes sintéticos, não quiescência real.

Suite operational reexecutada: 192 testes aprovados, sem skips, com o PHP/Config
oficial já disponível localmente. AST Python, links locais, Actionlint, scan de
secrets e diff-check dos três arquivos deste incremento aprovados. Não foram
instaladas dependências. Não se atribui homologação operacional a esses testes.

Regressões reexecutadas e aprovadas: sandbox 59, PR1 73, PR2 187 com 2 skips
Windows, PR3 71 com 3 skips Windows, PR5 144, IAM semântico 21, inventário 29,
PR6 146, storage guard 17, instalação 18 e controlador 13; static Docker e lint
PHP também aprovados. Os cinco skips nesta máquina não substituem nem anulam a
homologação POSIX anteriormente aceita. Nenhum teste Terraform foi executado.

## Estado preservado

Workflows de backup/deploy, Terraform, IAM, Ansible, Compose, contratos normativos
e guards publicados permanecem sem alterações. Nenhuma fábrica/launcher operacional
foi habilitada. Não houve commit, push, acesso AWS ou modificação na origem.
Depois da decisão, continuar a integração do supervisor, collector, lease remoto,
SSM, produtor e completion; o primeiro comando de backup operacional ainda não
existe e não deve ser substituído por uma execução do workflow bloqueado PR6.
