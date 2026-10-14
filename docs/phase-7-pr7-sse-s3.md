# PR7 — SSE-S3 e recuperação sem chave age

Decisão autorizada: o caminho operacional usa exclusivamente SSE-S3 (`AES256`),
TLS validado e IAM temporário. Não exige recipient, executável age, chave privada,
senha adicional, cofre ou repositório separado. Alterações preparadas localmente;
nenhuma operação AWS, captura ou restauração foi executada nesta revisão.

## Contrato vigente

Os novos conjuntos usam [manifest-v2](../contracts/recovery/schemas/manifest-v2.schema.json),
[completion-v2](../contracts/recovery/schemas/completion-v2.schema.json) e
[restore-evidence-v2](../contracts/recovery/schemas/restore-evidence-v2.schema.json).
O [inventário v1](../contracts/recovery/schemas/object-inventory-v1.schema.json)
permanece idêntico: sua estrutura não depende de criptografia cliente.
Contratos e fixtures v1 continuam legíveis pelo validador, exclusivamente para
compatibilidade histórica. Não existe conversão automática de backups age em SSE-S3.
O produtor operacional aceita somente v2; conjuntos de versões misturadas são rejeitados.

Os artefatos são `database.dump`, `configuration.tar`, `local_files.tar`,
`recovery_parameters.json` e `object_inventory.json`. Cada descritor exige
`sha256`, tamanho, bucket, key, VersionId e
`encryption={"method":"SSE-S3","algorithm":"AES256"}`.
O manifesto fica em `backups/<backup_id>/manifest.json`;
completion referencia `manifest_version_id` e `manifest_sha256`.
`private_decryption_key_required=false` é obrigatório no v2.
`required_secret_values_sse_s3=true` substitui o campo que exigia envelope age.

Consistência, identidade da instalação, fencing, drenagem, lease, journal,
manutenção original, paginação, delete markers e rejeição de versões ambíguas
permanecem obrigatórios. Inventários iguais não substituem controle de escritores.
Esta modalidade depende da preservação do bucket de dados e das versões referidas;
não protege contra perda completa daquele bucket.

## Fluxo operacional

1. Validar configuração privada v2, identidade/roles, TLS, região, versionamento e
   default AES256 dos buckets separados. Não descobrir credenciais da aplicação.
2. Obter lease, supervisor e controles administrativos de fencing existentes.
3. Capturar PostgreSQL/configuração/arquivos/parâmetros e inventários na mesma
   janela consistente. Restaurar somente a manutenção e os controles próprios.
4. Ainda sob lease, comparar tamanho e SHA-256 dos arquivos com o journal.
5. Enviar diretamente do host ao bucket de backups por HTTPS, sempre solicitando
   `ServerSideEncryption=AES256` no PUT e na abertura multipart. Plaintext não
   passa pelo runner, logs ou artifacts GitHub.
6. Ler a versão exata: exigir AES256, tamanho e SHA-256 do conteúdo baixado em
   streaming. ETag e metadados de hash não substituem leitura dos bytes.
7. Revalidar receipts, versões de dados e todos os artefatos antes do completion.
   Publicar completion por último, condicionalmente, com AES256; conferir também
   seus bytes, VersionId, histórico e SSE. ACK perdido exige reconciliação explícita.

SSE-S3 protege objetos em repouso; S3 devolve bytes descriptografados a identidades
autorizadas. Os arquivos de captura no host não recebem criptografia cliente:
permanecem nos diretórios root-only 0700, arquivos 0600 e EBS criptografado aprovado.
Não colocar staging em diretórios compartilhados ou artifacts. Um usuário com
GetObjectVersion autorizado pode ler configurações e segredos recuperáveis;
menor privilégio IAM, TLS e proteção do host continuam essenciais.
SSE não autentica autoria do manifesto e checksum não comprova restauração.
Fonte: [AWS SSE-S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingServerSideEncryption.html).

## Aplicação posterior, separada desta tarefa

Após revisão e autorização, publicar o patch e atualizar somente o código/contratos
e a configuração root-owned do host: `version=2`, `storage_encryption="SSE-S3"`,
removendo `recipient`, `recipient_id`, `age_path`, `age_sha256`. Configuração antiga
é rejeitada; não há fallback permissivo. A role Ansible deixa de exigir/copiar age.
Não é necessário apagar binários antigos ou chaves existentes para este patch.
Não gerar nenhuma chave age ou Secret de descriptografia.

Workflows operacionais permanecem administrativamente desabilitados. As pendências
de ownership/migração da bucket policy e ativação autorizada permanecem; este patch
não modifica IAM, Terraform, política S3 ou infraestrutura.

## PR8

Restauração futura obterá as versões exatas por IAM e TLS, validará contrato v2,
SHA-256/tamanhos/SSE e restaurará em destino independente. Nenhuma chave cliente ou
senha de descriptografia será solicitada. O PR8 não está implementado nem homologado
por esta alteração. Recuperação funcional exige ensaio real separado, sem destruir
a origem. Completion e validação de integridade nunca autorizam Destroy.

## Validador offline

`python -m scripts.backup --root DIRETORIO_PRIVADO --mode documents` valida v1/v2.
No modo `local`, v2 usa nomes fixos em `artifacts/`: os cinco artefatos acima,
`manifest.json` e, se houver evidência, `restore-report.json`.
JSONs de controle na raiz: `manifest.json`, `inventory.json`, `completion.json` e
`evidence.json` opcional. Keys S3 nunca viram caminhos locais.
O modo offline não comprova criptografia SSE nem existência remota de versões.
As fixtures são sintéticas e nenhum teste local equivale a backup operacional.

## Validação local desta alteração

| Suite | Aprovados | Skips | Observação |
| --- | ---: | ---: | --- |
| PR7 operacional | 385 | 3 | Inclui os 29 testes SSE-S3 novos; kernel Linux não executado no Windows |
| Contratos PR1 | 73 | 0 | Formato histórico v1 preservado |
| Validador PR2 | 187 | 2 | Testes POSIX não executados nesta rodada Windows |
| Captura PR3 | 71 | 3 | PHP/Config.php locais utilizados; subprocessos POSIX não executados |
| PR5 legado | 91 | 53 | Casos dependentes de age omitidos; age não é dependência do fluxo v2 |
| Orquestração PR6 | 146 | 0 | Inclui guards shell com Bash já disponível |
| IAM semântico | 21 | 0 | Planos mock offline já existentes; não é avaliação AWS |
| Controlador CI | 15 | 0 | Sem execução de deploy |
| Storage guard | 16 | 1 | Criação de symlink sintético indisponível no Windows |
| Instalação Nextcloud | 18 | 0 | PHP com adapters sintéticos |

Total: **1.023 aprovados e 62 skips**, nenhum failure/error nas invocações finais.
Não houve execução de Docker, qualificação remota Linux ou ensaio AWS nesta tarefa.
Homologações anteriores não são reapresentadas como execução deste patch.
Python/JSON/YAML, schemas, links locais, Actionlint, Gitleaks e diff-check foram
validados. Nenhuma dependência foi instalada ou chave gerada.

Cobertura SSE-S3 específica: contratos misturados/desconhecidos; requisitos de
chave indevidos; nomes de artefatos; ausência de AES256; body inválido; fechamento
do stream rejeitado; integridade local; upload simples/multipart; corrupção;
colisões com delete marker; ACK perdido sem retry; captura ausente/modificada;
manifesto produzido compatível com v2; completion prematuro/bloqueado; isolamento
dos simuladores e ausência de invocação age. Os testes do produtor e publicação
usam colaboradores sintéticos e não constituem evidência de backup real.

## Escopo para revisão

32 caminhos locais, incluindo as duas remoções do adapter age operacional e seus
testes substituídos; nenhum staging, commit ou push:

```text
ansible/roles/recovery_operational/tasks/main.yml
contracts/recovery/README.md
contracts/recovery/fixtures/sse-s3-v2/complete-unproven.json
contracts/recovery/fixtures/sse-s3-v2/completion.json
contracts/recovery/fixtures/sse-s3-v2/object-inventory.json
contracts/recovery/fixtures/sse-s3-v2/restore-evidence.json
contracts/recovery/schemas/completion-v2.schema.json
contracts/recovery/schemas/manifest-v2.schema.json
contracts/recovery/schemas/restore-evidence-v2.schema.json
docs/README.md
docs/phase-7-pr7-integrated.md
docs/phase-7-pr7-sse-s3.md
docs/phase-7.md
scripts/backup/README.md
scripts/backup/__main__.py
scripts/backup/integrity.py
scripts/backup/operational/completion.py
scripts/backup/operational/encryption.py                 # removido
scripts/backup/operational/host_worker.py
scripts/backup/operational/runtime_config.py
scripts/backup/operational/s3.py
scripts/backup/operational/tests/test_completion_operational.py
scripts/backup/operational/tests/test_encryption.py       # removido/substituído
scripts/backup/operational/tests/test_integration.py
scripts/backup/operational/tests/test_s3.py
scripts/backup/operational/tests/test_sse_s3.py
scripts/backup/operational/tests/test_transport.py
scripts/backup/operational/transport.py
scripts/backup/schemas.py
scripts/backup/sealing/transport.py
scripts/backup/semantics.py
scripts/backup/validator.py
```

Terraform/IAM, workflows, Compose/aplicação, módulos de captura, supervisor,
lease, fencing e schemas v1 permanecem sem mudanças. O compartilhamento do
algoritmo de transporte conserva os defaults restritivos do simulador legado;
o adapter operacional separado seleciona nomes/hash v2 fixos, sem seleção por
Environment ou flag de habilitação.
