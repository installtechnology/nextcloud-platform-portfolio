# Validador offline — recuperação v1

Também valida **v2 SSE-S3**, conforme [contrato vigente](../../docs/phase-7-pr7-sse-s3.md).
Para v2, o modo local lê `artifacts/` com nomes `.dump`, `.tar`, `.json` fixos,
sem descriptografia age. A disposição `ciphertext/` abaixo é exclusiva do v1.

Este documento descreve `python -m scripts.backup`. A interface separada de
[captura local PR3](capture/README.md) permanece limitada a ensaios isolados.

PR2 da Fase 7. Esta ferramenta somente lê arquivos locais. Não acessa AWS,
S3, EC2, rede, Terraform ou aplicações; não executa subprocessos, backup,
descriptografia, restauração ou Destroy. Os [contratos do PR1](../../contracts/recovery/README.md)
permanecem normativos e inalterados.

## Instalação e execução

Python **3.10 ou superior**. Prepare as dependências em um ambiente de
desenvolvimento, antes de trabalhar offline:

```text
python -m pip install -r scripts/backup/requirements.txt
python -m scripts.backup --root DIRETORIO_LOCAL --mode documents
python -m scripts.backup --root DIRETORIO_LOCAL --mode local
python -m unittest discover -s scripts/backup/tests -p "test_*.py" -v
```

Instalar dependências pode exigir rede; o validador não instala dependências
automaticamente e sua execução não exige rede. Os testes usam somente fixtures
sintéticas, arquivos temporários e a biblioteca de schemas previamente instalada.

Execute na raiz do repositório. `--root` é o único caminho que pode ser absoluto:
indica um diretório local autorizado, privado e estável, sob controle do operador.
Não use diretórios sincronizados, compartilhamentos de rede, links ou diretórios
modificados simultaneamente por outros processos. Proteja por permissões do SO os
JSONs já descriptografados; o PR2 não prepara nem descriptografa esses documentos.

O diretório deve conter:

```text
manifest.json
inventory.json
completion.json
evidence.json                     # opcional; se existir, deve ser válido
ciphertext/                       # obrigatório somente no modo local
  manifest.json.age
  database.age
  configuration.age
  local_files.age
  recovery_parameters.age
  object_inventory.age
  restore-report.age              # obrigatório se evidence estiver presente
```

`--manifest`, `--inventory`, `--completion` e `--evidence` aceitam apenas caminhos
relativos seguros sob `--root`. Uma evidência explicitamente indicada e ausente
é erro. Sem `--evidence`, `evidence.json` é detectado automaticamente; arquivo
presente malformado nunca é ignorado. Outros nomes exigem indicação explícita.
Não há descoberta de outros backups nem fallback para `latest`.

Os nomes locais dos ciphertexts são fixos. **Keys e version IDs S3 não são
caminhos do sistema de arquivos** e nunca são usados para abrir arquivos. O
operador deverá disponibilizar as versões exatas por um procedimento futuro,
fora deste PR; a origem/version ID dos bytes não pode ser autenticada offline.

## Níveis e resultados

`documents` valida os quatro JSON Schemas locais e as relações normativas:
identidade, captura, source, bucket separado, destinatário de criptografia,
prefixos, versões obrigatórias, inventário completo declarado, checksums S3
descritos corretamente e sequência temporal. Não confere bytes de artefatos.

`local` é o padrão e exige todos os ciphertexts: os cinco artefatos do manifesto,
o manifesto indicado pelo completion marker e o relatório se houver evidência.
Confere tamanhos declarados e SHA-256 em blocos de 1 MiB. O contrato não declara
tamanho do manifesto ou do relatório; nesses dois casos confere hash, arquivo
não vazio e limites locais. Qualquer arquivo obrigatório ausente reprova o modo.

Saída JSON sanitizada, sem valores de entrada, paths, nomes de buckets,
identificadores operacionais ou mensagens brutas das bibliotecas. Exemplo:

```json
{"ok": true, "conformity": "passed", "local_integrity": "not_checked", "functional_restore": "not_verified"}
```

O exemplo mostra somente os campos centrais; a saída completa também informa
artefatos verificados, estado da declaração de evidência e limitações. `ok=true`
significa sucesso **no nível selecionado**, nunca autorização operacional.

| Exit code | Significado |
| --- | --- |
| 0 | Nível selecionado aprovado |
| 2 | Entrada, JSON, schema, path, limite ou estabilidade da entrada inválidos |
| 3 | Inconsistência semântica entre documentos |
| 4 | Falha na integridade/leitura dos ciphertexts |
| 5 | Dependência, schema local ou erro interno; nenhuma aprovação |
| 130 | Interrupção; nenhuma aprovação |

Falhas retornam `code`, opcionalmente `document` (papel fixo) e `keyword`
(regra do schema), sem conteúdo sensível. Exemplos: `DUPLICATE_JSON_KEY`,
`SCHEMA_INVALID`, `MIXED_INVENTORY`, `TIME_ORDER`, `FILE_MISSING`,
`ARTIFACT_SIZE_MISMATCH`, `ARTIFACT_HASH_MISMATCH`, `UNSAFE_PATH` e `FILE_CHANGED`.
É responsabilidade do chamador não persistir a linha de comando contendo paths
privados; senhas não são argumentos suportados.

## Segurança e limites

Limites fixos do CLI: 8 MiB por JSON, profundidade 32, 100.000 nós,
4.096 caracteres por string, 20.000 itens por array, 10.000 objetos de inventário,
128 dígitos por número inteiro, 8 GiB por ciphertext e 32 GiB no total.
Uma captura maior exige revisão futura desses limites; não é aceita silenciosamente.
O limite de nós/profundidade é examinado antes da desserialização, evitando que
um array com muitos elementos seja materializado antes da rejeição. O consumo
de RAM também inclui o texto, o JSON e os índices; não existe promessa de um
teto de RAM igual ao limite de bytes do arquivo.
Campos numéricos v1 são inteiros: a leitura evita arredondamento de floats e
aceita representações decimais/exponenciais somente quando exatamente integrais.

UTF-8 estrito, objetos JSON obrigatórios, chaves duplicadas rejeitadas em todos
os níveis, formatos RFC3339 obrigatórios e propriedades indevidas rejeitadas.
Schemas são os quatro arquivos locais fixos; referências `$ref`/`$dynamicRef`
são proibidas. Não há resolução remota. `uniqueItems` usa comparação de igualdade
JSON com conjunto indexado, evitando comparação quadrática de inventários.

Recusa traversal, paths absolutos de documentos, UNC, ADS Windows, nomes de
dispositivos, symlinks, reparse points, hardlinks e arquivos não regulares.
No POSIX usa `dir_fd`/`O_NOFOLLOW`; no Windows usa handles sem compartilhamento
de escrita/exclusão e conferência do caminho final antes da leitura. Falha
fechada em plataformas sem essas primitivas. Compara identidade/tamanho/tempos
antes/depois da leitura e novamente ao final. Isso não substitui isolamento e
permissões do diretório contra um adversário que controle o sistema operacional.
Leitura incompleta de JSON e divergência entre tamanho inspecionado e tamanho
efetivamente processado do ciphertext são erros. O aparecimento de uma evidência
opcional durante a validação também reprova o conjunto. A rechecagem final da
estabilidade das entradas usa exit code 2; falhas durante o hashing usam 4.

**Linux ainda não foi executado nesta auditoria:** o WSL não possui distribuição
instalada e Docker não está disponível. O validador **não poderá ser utilizado
operacionalmente no Linux antes da validação POSIX**. Para essa validação,
execute as três suítes offline (validador, contratos e regressões de deploy),
incluindo os testes reais POSIX de FIFO/socket e truncamento concorrente.
Também exercite symlinks, hardlinks e `dir_fd`/`O_NOFOLLOW` no filesystem escolhido.
Mocks e revisão estática não substituem essa execução. Nenhum componente foi
instalado para contornar essa limitação.

## O que não foi comprovado

- Checksums válidos **não comprovam restauração funcional**. Mesmo uma evidência
  conforme continua sendo uma declaração, não um teste de restore executado aqui.
- Não verifica autenticação/assinatura do produtor, descriptografia age, vínculo
  entre os JSONs fornecidos e o conteúdo dos ciphertexts, SQL, archives ou aplicação.
- Não comprova quiescência real, completude real do listing S3, autenticidade de
  imagens, existência das versões ou permissões AWS. Confere as declarações e
  relações; a projeção/hash do inventário não é recalculada sem uma serialização
  canônica definida pelo contrato.
- Esta modalidade depende da preservação do bucket de dados e suas versões;
  não protege contra perda completa desse bucket.
- Não cria autorização para Destroy. Isso exige validação funcional isolada,
  critérios e autorização próprios em etapas futuras.

Veja o [relatório do PR2](../../docs/phase-7-pr2.md) para cobertura e validações.
