# Fase 7 — PR2: validador offline

Implementação local baseada no [contrato v1](../contracts/recovery/README.md),
sem alterar seus quatro schemas ou regras normativas. Nenhuma operação AWS,
backup, restore, deploy, Terraform ou mudança operacional foi realizada.

## Arquitetura

| Módulo em scripts/backup | Responsabilidade |
| --- | --- |
| `__main__.py` | CLI, JSON sanitizado e exit codes |
| `validator.py` | Orquestração dos níveis documents/local |
| `safe_files.py` | Leitura confinada e detecção de links/mudanças |
| `json_input.py` / `limits.py` | Parser estrito e limites de recursos |
| `schemas.py` | Quatro schemas locais, formatos e unicidade eficiente |
| `semantics.py` | Relações de identidade, captura, armazenamento e tempo |
| `integrity.py` | Tamanhos e SHA-256 em streaming de ciphertexts |
| `errors.py` | Falhas com códigos fixos e sem conteúdo sensível |

Os documentos são validados estruturalmente antes das relações semânticas.
O modo local só confere artefatos após ambas as etapas. Nenhum estado de sucesso
é produzido antes de concluir todas as verificações selecionadas.

## Cobertura

Os testes sintéticos executam os 46 contraexemplos do PR1 contra o validador:
24 erros de schema e 22 inconsistências semânticas, com verificação do keyword
ou reason code esperado. As 73 regressões do contrato também são executadas
contra o motor de produção, além da execução separada dos testes originais.

Testes adicionais cobrem níveis de verificação, sete ciphertexts, omissão da
evidência, ausência/corrupção de arquivos, limites, streaming, alterações durante
a leitura, hardlinks/symlinks, duplicação JSON, UTF-8, profundidade, números
ambíguos, traversal e saída sanitizada. Timestamps com precisão de nanossegundos
não são truncados para comparar a ordem. Socket e subprocessos são bloqueados
por mocks em uma execução local completa.

As fixtures do contrato permanecem intocadas. Testes criam ciphertexts
**sintéticos**, sem envelope age real, para comprovar comparação de bytes; isso
não representa teste de criptografia, backup real ou restauração.

## Aceite e pendências

Auditoria final local em Windows: **189 testes do validador**, com **187 aprovados
e 2 skips POSIX explícitos**, **73 testes originais** e **13 regressões offline
de deploy** aprovados. Os 189 incluem uma
reexecução das 73 regressões do PR1 contra o motor de produção; não representam
189 cenários inteiramente independentes dos contratos. Sintaxe Python,
dependências fixadas, 29 links locais, scan de secrets e `git diff --check`
(incluindo arquivos untracked) aprovados. O CLI também passou com as fixtures
no modo documents. Nenhum desses resultados comprova restauração funcional.

Os testes de handles Windows exigiram execução local fora do sandbox, que
bloqueia a API de inspeção do caminho final. A proteção permaneceu ativa.
O WSL está disponível como comando, mas não possui distribuição instalada;
Docker não está disponível. Nenhum componente foi instalado. O ramo POSIX
ainda requer execução em Linux **antes do uso operacional nesse sistema**.
O validador **não poderá ser utilizado operacionalmente no Linux antes dessa
validação POSIX**. Os dois skips são truncamento
concorrente real e arquivos especiais FIFO/socket Unix. Os testes portáveis
de truncamento, crescimento, limites, symlinks/hardlinks e metadados passaram,
assim como o teste Windows de bloqueio de escrita durante a leitura.
Não houve acesso a serviços externos.

Correções comprovadas na auditoria: limite de nós JSON antes da materialização,
rejeição de leitura curta mesmo com metadados aparentemente estáveis, comparação
do tamanho retornado pelo streaming com o tamanho inspecionado e recusa de uma
evidência opcional criada no meio da validação. Cada caso tem regressão específica.
Não foram alterados contratos normativos, operações ou permissões.

Ainda precisam de etapas futuras autorizadas: materialização das versões S3,
criptografia/descriptografia, consistência operacional, validação de dump e
restauração isolada interrompível e repetível. Hashes locais não comprovam nenhum
desses pontos. O PR2 não determina elegibilidade para Destroy.

Execução, estrutura local, limites e interpretação completa dos resultados:
[scripts/backup/README.md](../scripts/backup/README.md).
