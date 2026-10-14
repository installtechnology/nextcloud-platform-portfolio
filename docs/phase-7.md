# Fase 7 — Contrato de Recuperação (PR1)

**Atualização autorizada do PR7:** novos conjuntos usam o
[contrato v2 SSE-S3](phase-7-pr7-sse-s3.md), sem gestão de chave age.
As exigências de age abaixo descrevem o v1 histórico; garantias de consistência,
integridade, dependência do bucket de dados e restauração independente permanecem.

Estado: contrato, schemas, fixtures sintéticas e testes locais. **Nenhum backup ou
restore operacional implementado ou executado.** A Fase 6 permanece concluída.
Esse estado refere-se à entrega original do PR1. Posteriormente, PR7/PR8
executaram backup e restore; em 14/10/2026, o [core foi recuperado na EC2 recriada](phase-7-reprovisioning.md)
com login/download/hash verificados e intervenções documentadas. Os flags formais
do contrato não foram promovidos automaticamente.
PR2 e demais entregas exigem autorização própria. Este documento é normativo para
as próximas implementações; campos declarados no manifesto precisam ser
comprovados pelo produtor/verificador, não apenas preenchidos com `true`.

## Garantias distintas

| Classificação | Evidência exigida | O que não comprova |
| --- | --- | --- |
| Documento estruturalmente válido | JSON Schema v1 e relações semânticas válidas | Artefatos existentes, captura consistente ou restore |
| Backup completo e íntegro | Conjunto fechado, captura consistente, artefatos obrigatórios, leitura remota e hashes verificados | Recuperação funcional, chave privada disponível ou sobrevivência futura das versões |
| Backup com restauração comprovada | Evidência separada de restore funcional isolado, vinculada ao manifesto exato | Garantia permanente, RPO/RTO universal ou autorização para Destroy |

Checksum comprova igualdade de bytes dentro do método de verificação; não comprova
que SQL pode ser restaurado, que os arquivos correspondem ao banco ou que o usuário
consegue entrar e baixar conteúdo. `pg_restore --list` também não é restore.
O manifesto completo mantém `verification.restore_proven=false` para sempre.
Provas posteriores ficam em `restore-evidence`, sem reescrever o manifesto.
Nenhuma fixture deste PR é evidência operacional ou permite Destroy.

## Modalidade v1 e dependências

`version_referenced_data_bucket` retém PostgreSQL, configuração, arquivos locais
mutáveis e parâmetros protegidos no bucket de backups, mas referencia os conteúdos
pelos VersionIds do **bucket original de dados**. Exige que esse bucket e todas as
versões referenciadas sobrevivam e permaneçam legíveis.

**Não protege contra perda completa do bucket de dados**, exclusão permanente das
versões ou perda de acesso à conta. Não há cópia independente dos objetos nesta
modalidade. State, dados e backups permanecem separados. Perda de uma dependência
invalida a possibilidade de recuperação, mesmo com dump e checksums corretos.
Um modo com cópia independente, Object Lock, outra conta/região ou KMS necessita
decisão e contrato próprios; não está implícito no v1.

O formato v1 limita compatibilidade a Nextcloud 34 Apache e PostgreSQL major 17
Bookworm, as variantes atualmente fixadas pelo Compose, sem
autorizar upgrades. Patch, digest, commit, apps e parâmetros precisam ser conferidos
antes de restore; mudança incompatível exige nova revisão do contrato.
Tag Nextcloud deve concordar com nextcloud_version. Sintaxe de digest é verificada,
mas autenticidade da imagem e compatibilidade com o destino não vêm do schema.

## Conteúdo obrigatório do conjunto

| Artefato | Conteúdo dentro do envelope criptografado |
| --- | --- |
| `database` | Dump custom integral do banco da aplicação; schema, registros, sequências e objetos necessários, produzido com cliente major 17 compatível |
| `configuration` | Todo `config/`, incluindo config.php, todos os *.config.php, identidade/segredos gerados e `.platform-storage.json`; permissões e ownership |
| `local_files` | Dados locais relevantes, apps adicionais e temas de nextcloud_html; inventário completo do volume e justificativa de exclusões |
| `recovery_parameters` | Banco/owner/extensões/encoding/collation/grants necessários, versões/apps, parâmetros Compose e secrets necessários à recuperação |
| `object_inventory` | Inventário paginado de chaves/VersionIds visíveis e ausências no ponto consistente |

Arquivos locais não podem ser presumidos descartáveis só porque Primary Object
Storage é S3. O inventário operacional decidirá inclusões; até isso ocorrer, nenhuma
exclusão de dado mutável é aceita. Logs/cache só poderão ser excluídos com classificação
documentada. Não copiar PGDATA em execução como substituto do dump lógico.
Roles globais não vêm automaticamente no dump do banco: reconstrução do owner,
grants e extensões deve estar especificada nos parâmetros. Credenciais PostgreSQL e
Redis, além da identidade Nextcloud, precisam ser recuperáveis sem a EC2 original.
GitHub Secrets não fornece leitura posterior de valores e não é a única custódia.

Redis é cache/locking temporário, não fonte a restaurar. Volumes de observabilidade
são excluídos por decisão do projeto; dashboards provisionados vêm do Git e alterações
somente na UI do Grafana não são cobertas. State, tfvars/backend privados, chave SSH
e logs brutos do runner não entram no backup da aplicação.

## Consistência PostgreSQL / Nextcloud / S3

Uma única janela, identificada por `consistency.id`, deve cobrir os cinco artefatos.
A implementação futura deverá:

1. Adquirir exclusão mútua no ambiente e registrar o estado original de serviço/manutenção.
2. Bloquear novos escritores, drenar requisições em andamento e controlar tarefas de
   background, cron e escritores externos. Apenas maintenance=true não basta.
3. Com os escritores quiescentes, capturar dump, configuração e arquivos locais,
   mantendo o bucket sem escritas durante toda a captura.
4. Inventariar completamente S3 antes/depois e comparar a projeção canônica de
   chaves, versões, tamanhos, ETags, checksums disponíveis e ausências. Os hashes
   `inventory_before_sha256`/`inventory_after_sha256` devem ser idênticos.
5. Retomar o estado original e confirmar seu resultado. Nextcloud em manutenção
   ficará unhealthy pelo probe atual; não usar Compose wait como gate nessa janela.
6. Verificar os artefatos transferidos e dependências antes de concluir o conjunto.

O horário do dump sozinho não é um snapshot distribuído. Ordenação normativa:
horário inicial codificado no backup_id <= quiesced_at <= capture_finished_at <=
created_at <= completed_at. Não tolerar mistura de janelas ou alteração de inventário.
Hashes iguais são necessários, mas não provam ausência de um escritor que modificou
e depois reverteu conteúdo: a exclusão dos escritores também deve ser comprovada.

O inventário v1 usa uma versão visível por chave. `absent_current_keys` registra
chaves cuja versão corrente é delete marker; não são conteúdo a reativar. Chaves
nunca existentes também estão ausentes. Em restore isolado, o conjunto de chaves
visíveis deve ser exatamente o inventariado: não importar objetos mais novos nem
ressuscitar versões cobertas por delete markers. Versões históricas não selecionadas
não são copiadas automaticamente para o destino. Listagem precisa terminar sem
truncamento; VersionId ausente, vazio, `null`, com whitespace ou controles bloqueia
conclusão. IDs são opacos; não interpretar como hashes nem normalizar versões recebidas.

ETag é informação opaca, não SHA-256. `checksum=null` reconhece ausência de checksum
S3 disponível, sem inventar uma verificação de conteúdo. O restore deverá calcular
e conferir hashes funcionais dos arquivos de teste; checksums compostos e completos
não são equivalentes. O descritor opcional contém digest em Base64 canônico e o tipo
de checksum; COMPOSITE exige part_count e não permite CRC64NVME. Sufixo de contagem
da resposta S3, quando presente, deve ser preservado no campo part_count separado,
não dentro de value_base64. FULL_OBJECT não carrega part_count. O tamanho do digest
precisa corresponder ao algoritmo. Isso valida metadados, não os bytes do objeto.
[Referência AWS dos tipos de checksum](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html).
SHA-256 dos artefatos neste manifesto se refere ao ciphertext.

## Identidade, manifesto e conclusão

Schemas [v1](../contracts/recovery/README.md), JSON Schema Draft 2020-12, sem refs
remotas. Objetos fechados (`additionalProperties=false`) evitam campos arbitrários
ou secrets adicionados ao manifesto público por engano. O manifesto inteiro é
sensível e será criptografado; esse controle estrutural não sanitiza seus valores.

`backup_id`: UTC compacto + UUID v4. Cada execução escreve um prefixo próprio
`backups/<backup_id>/`; nenhum ID `latest`, overwrite ou mistura entre runs.
Todos os artefatos carregam o mesmo backup_id/consistency_id, ficam no mesmo bucket
de backups distinto de dados/state e têm chave exclusiva, VersionId, tamanho e hash.
Todos usam o mesmo recipient_id; manifesto e relatório de restore usam esse mesmo
destinatário e o bucket de backups inferido dos artefatos. Marker e relatório não
possuem outro campo de bucket: não aceitar override de destino ao resolver essas chaves.

O manifesto completo será criptografado em `manifest.json.age`. O schema prevê
envelope `age-v1` com identificador do destinatário, sem chave privada; implementação,
custódia, compatibilidade e testes criptográficos ficam para PRs seguintes.
Chave privada de recuperação fica fora da EC2 descartável e do conjunto criptografado.
Secrets em archives/parametrização ficam protegidos; não em argv, logs, summaries,
Git ou artifacts GitHub. Nenhuma criptografia foi implementada neste PR.

O marcador `completion` referencia **versão e hash do manifesto criptografado** e
é publicado por último. Não é autorização de restore/Destroy nem assinatura digital.
O schema do manifesto representa somente conjuntos concluídos: um upload parcial
não ganha manifesto completo/marker. Journal de execução/interrupção será interno,
não uma variante silenciosa desse schema. Ausência do marker, manifesto ilegível ou
qualquer artefato ausente faz o conjunto permanecer incompleto.
Nem manifesto, marker, evidência JSON ou aprovação dos testes é uma autorização de
Destroy. Não existe avaliador de autorização, função de elegibilidade ou workflow de
Destroy neste PR. O futuro gate exigirá validação independente, prova funcional
autenticada/revisada, dependências revalidadas e autorização humana específica.

## Regras de rejeição entre documentos

Além do JSON Schema, o verificador futuro deve rejeitar:

- IDs/janelas/source divergentes entre manifesto, inventário, marker e evidência;
- chave fora do prefixo exato, artefato duplicado ou bucket errado;
- contagem diferente da lista, chave S3 duplicada ou visível e ausente simultaneamente;
- inventário antes/depois divergente, captura incompleta ou escritores não quiescentes;
- arquivo ausente, versão inacessível, hash/tamanho divergente, dump inválido ou envelope ilegível;
- incompatibilidade de versão, digest, apps, owner, storage ou parâmetros necessários;
- chave privada indisponível, dependência S3 perdida ou tentativa de juntar backups;
- restore sobre o ambiente original ou remapeamento de storage sem aprovação explícita.

JSON Schema não compara campos entre documentos, calcula hashes ou acessa S3. Os
testes do PR1 contêm um oráculo sintético dessas relações; **não são o verificador
de PR2**. Booleanos no manifesto são declarações a comprovar. Conteúdo SQL, archives,
permissões, formato criptográfico e existência AWS não são verificados por schema.

## Preparação para restauração isolada

Evidência separada vincula backup_id, source e hash do manifesto exato, janela do
ensaio, identidade do destino e relatório criptografado versionado. Não alterar
manifesto original para alegar sucesso. Destino deve ter banco/volumes/bucket isolados,
sem credenciais capazes de modificar dados do ambiente original.

Como o guard atual proíbe troca silenciosa de bucket, o futuro fluxo de restore terá
remapeamento explícito aprovado e testado. O campo de aprovação apenas registra esse
fato; não desliga o guard nem implementa autorização. Nenhum hook será executado aqui.
Configuração, banco e objetos precisam ser preparados antes da inicialização; o
instalador inicial nunca pode ser usado para restaurar banco/objetos já existentes.

Para comprovar funcionalidade: descriptografar, restaurar banco e configuração,
mapear versões exatas, confirmar guard/installed=true, login e hashes de conteúdo,
apps e integração PostgreSQL/Redis; interromper/retomar o ensaio e repetir sem reinstalar.
Evidência deve ser autenticada/revisada pelo operador ou sistema de validação futuro.
Um JSON preenchido com todos os checks=true, especialmente uma fixture, não prova isso.
RPO/RTO serão medidos no ensaio e registrados no relatório, não inferidos pelo schema.

Destroy/DR continuam bloqueados até backup completo/íntegro, dependências disponíveis,
restore comprovado aplicável e autorização separada. Nenhum workflow de Destroy é
criado por esse contrato. Restore específico nunca cai silenciosamente em latest.

## Interrupções e sequência de entregas

Falha não publica sucesso e não exclui dados/versões/conjuntos. Retomada deve conferir
ownership, ID, hashes e estado anterior; não concatenar artefatos de outro run. O
supervisor/journal local e retorno seguro da manutenção serão implementados/testados
depois. Se não houver segurança para retomar o serviço, emitir diagnóstico sanitizado
e exigir intervenção. Cleanup do runner não garante recuperação sozinho.

| PR | Entrega | Dependência |
| --- | --- | --- |
| 1 (este) | Contrato, schemas, fixtures e testes locais | Auditoria aprovada |
| 2 | Verificador offline de artefatos/relações e integridade | PR1 aprovado |
| 3 | Captura local consistente e controle de interrupção | PR1/2 |
| 4 | IAM OIDC de backup, separado de deploy/EC2 | Contrato; apply somente autorizado |
| 5 | Inventário S3, criptografia e transferência/conclusão | PR2/3/4 |
| 6 | Workflow manual backup, sem Terraform/deploy/destroy | PR3–5 |
| 7 | Primeiro backup real em janela autorizada | PR6 |
| 8 | Restore isolado e evidências funcionais | Conjunto real verificado |

Sem lifecycle/expiração/exclusão automática. Custos dependem do crescimento dos
dumps/configurações, versões S3, requests, runner e futuro ensaio isolado. Não há
estimativa financeira sem medição. Observabilidade/backup de bucket independente,
Destroy, DR e Azure não foram implementados neste PR.

## Aceite e validação local

- Schemas estruturalmente válidos e fixtures positivas aceitas, com format checker ativo.
- Exemplos negativos rejeitados na camada certa (schema ou relações normativas).
- Distinção explícita entre completo/íntegro e restore comprovado; fonte S3 não independente.
- Ensaio preparado para destino isolado, sem executar instalação, SQL, SSH ou AWS.
- Nenhuma alteração em Terraform/IAM/Ansible/Compose/workflow operacional.

Comandos e limites dos testes em [contratos/recovery](../contracts/recovery/README.md).
Rollback do PR1 é reverter apenas docs/contratos; não há mudança operacional a desfazer.

### Resultados locais — 2026-10-09

Windows, Python 3.11: **41 testes do contrato aprovados**, incluindo quatro
meta-schemas, fixtures positivas, 26 mutações negativas e demais limites de garantia.
**13 testes offline existentes do controlador de deploy aprovados**, usando mocks,
sem Terraform/SSH/AWS reais. Sintaxe Python e JSON sem chaves duplicadas aprovadas.
Links locais dos documentos alterados, Gitleaks redigido e diff-check aprovados.
Não houve teste Linux, de criptografia, PostgreSQL real, backup real ou restore.

Problema encontrado e corrigido: jsonschema sem o validador RFC3339 opcional aceitava
uma data impossível. requirements inclui rfc3339-validator e a suíte exige o checker
ativo antes de iniciar. A regressão de data inválida passou depois da correção.
Dependências usadas nesta revisão ficam em diretório local ignorado, fora do deploy.

### Auditoria final do PR1 — 2026-10-09

Os 16 arquivos (incluindo todos os untracked) foram revistos. As cópias dos tipos
backup_id/source/VersionId nos quatro schemas são comparadas por teste para impedir
divergência; $id são únicos e não há $ref/$dynamicRef remoto ou local a resolver.
O URI $schema identifica o dialeto; o validador usa o meta-schema instalado localmente.

Corrigidos: âncora de regex que aceitava newline final, VersionId whitespace, tag da
imagem divergente da versão declarada, calendário inválido dentro de backup_id,
destinatários diferentes, descritores de checksum malformados e testes negativos
que não exigiam o motivo exato. O caso de chave duplicada agora mantém object_count
correto para não depender de uma segunda falha acidental.

Resultado revisado: **73 testes locais do contrato**, com **46 mutações negativas**,
mais **13 testes offline existentes do controlador**. Esta contagem substitui os 41
da primeira execução; não representa testes operacionais adicionais.
As 86 expressões regulares dos schemas também foram compiladas em ECMAScript;
fim absoluto rejeita newline final. Links locais, scan redigido de secrets e
diff-check (incluindo arquivos untracked) foram reexecutados e aprovados.

| Cobertura exercitada | Limite explícito |
| --- | --- |
| Quatro meta-schemas, exemplos positivos e definições compartilhadas | Não consulta registry, AWS ou imagens reais |
| IDs/janelas/source/prefixos, duplicatas, contagens, versões vazias/null/whitespace e calendário UTC | Não comprova existência de VersionIds nem estabilidade real dos escritores |
| Presença dos cinco artefatos e declarações de integridade/captura | Não lê dump/archive nem calcula hash de um backup real |
| Tags/digests sintáticos, checksum Base64/tipo/tamanho e inventário completo declarado | Não valida compatibilidade funcional ou conteúdo dos objetos |
| Evidência separada, destino diferente, vínculo ao conjunto e cada check obrigatório | Não executa restore; todos os checks são declarações |
| Manifesto completo sem prova, marker ausente e campos destroy_authorized rejeitados | Não implementa ou testa um gate operacional de Destroy |

Permanece obrigatório resolver autenticação da prova, custódia da chave, disponibilidade
das dependências, arquivos locais relevantes e isolamento real do destino nos PRs
posteriores. Código semântico deste PR vive apenas no teste; não é o verificador de PR2.
