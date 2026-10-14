# PR5 — Interfaces locais de preparação e simulação

Não há cliente AWS, descoberta de credenciais, workflow, entrada operacional ou
integração ao deploy. `MemoryS3` é um simulador em memória. `SimulatedSeal` exige
simulador e `Store(synthetic=True)`; sua conclusão retorna `backup_complete=false`,
`operationally_homologated=false` e `functional_restore=not_verified`.
Nunca usar essa conclusão para autorizar Destroy. O PR3 continua
`local_candidate_only`, inclusive após os testes de completion.

## Responsabilidades

| Módulo | Responsabilidade |
| --- | --- |
| `inventory.py` | Paginação completa, visibilidade corrente, versões, checksums e projeção |
| `hooks.py` | Duas observações dentro da janela controlada pelo PR3, com atestação de escritores |
| `crypto.py` | Criptografia streaming com age oficial fixado; recebe somente destinatário público |
| `transport.py` | Intents duráveis, PUT/multipart condicionais, SHA-256 e GET de versão específica |
| `completion.py` | Conferência do conjunto, manifesto cifrado e completion publicado por último |
| `simulator.py` | Protocolo S3 sintético, sem rede e com memória limitada |

O ponto de integração Python é `Capture(store, backend, inventory_hooks=observer)`.
Sem esse argumento, o fluxo anterior é preservado. A CLI do PR3 não ativa hooks.
As interfaces são exercitadas pelos testes; não existe comando para backup AWS.
Após captura sintética com hooks, `SimulatedSeal(...).seal(operation_id)` exercita
o fluxo completo contra o simulador. `Transport.inspect_interrupted()` verifica
somente versões conhecidas; não retoma nem publica completion.

## Projeção determinística v1

UTF-8, JSON compacto, chaves de objetos ordenadas, sem normalização Unicode ou
de VersionId/ETag. O objeto projetado contém `projection_version=1`, `source`,
`objects` e `absent_current`. Os dois arrays são ordenados pelos bytes UTF-8 da
key. Objetos conservam key, VersionId, tamanho, ETag e descriptor de checksum.
Ausências conservam a key **e o VersionId do delete marker corrente** no receipt
interno. O inventário normativo mantém `absent_current_keys`, conforme PR1.
IDs da operação, consistência e horários não integram a projeção; continuam
vinculados e conferidos nos documentos/receipts. O hash é SHA-256 desses bytes.
O receipt registra início/fim UTC de cada observação; a sequência temporal inteira
é conferida dentro da janela PR3 antes de qualquer transferência. Inventários
históricos são paginados para selecionar e verificar versões correntes; o contrato
v1 não inclui todas as versões históricas na projeção. Não há normalização Unicode:
duas keys visualmente iguais podem continuar sendo keys distintas.

Dois hashes iguais não demonstram ausência de escritores. O attestor injetado
deve verificar, antes/depois de cada listagem, fences da aplicação, jobs de fundo,
escritores externos, drenagem e manutenção contínua do mesmo fence. Não existe
attestor operacional neste PR. Os testes apenas comprovam a ordem do mock e a
rejeição de atestações ausentes/incompatíveis. ListObjectVersions não é snapshot.

## Protocolo injetado e limites

`list_versions(bucket, prefix, cursor)` retorna páginas equivalentes à API S3,
com keys já decodificadas, `IsTruncated` booleano e ambos os markers para próxima
página. `head(bucket, key, version)` retorna VersionId, ContentLength, ETag e
checksum quando disponível. `get(...)` fornece stream fechado pelo consumidor.
Operações de escrita: `put`, `create_multipart`, `upload_part`, `list_parts`,
`complete_multipart` e `abort`. Nenhum método de exclusão de objetos existe.

Um eventual adapter SDK precisará de aprovação/implementação posterior: owner
esperado, TLS, timeouts de conexão/leitura, retries controlados, paginação completa
de partes e parsing de erros embutidos na resposta CompleteMultipartUpload. Não
há verificação real de região, bucket owner, criptografia padrão, versionamento,
IAM, OIDC ou conectividade neste PR. O protocolo não é substituto desses checks.
O simulador rejeita respostas com Error, agrupamentos por delimiter/CommonPrefixes,
continuação inválida e regressão de keys entre páginas. Um adapter não pode afirmar
completude após falha de parsing ou erro XML em HTTP 200.
[ListObjectVersions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectVersions.html).

Limites: 1.000 páginas, 20.000 registros de versões, 10.000 keys, receipts até
512 KiB, JSON sujeito aos limites do PR2, 8 GiB por artefato, chunks de hash de
1 MiB, partes de 8 MiB, limiar multipart de 16 MiB e até três tentativas de parte.
PUT simples é limitado a 64 MiB. O simulador limita conteúdo a 64 MiB por objeto
e 128 MiB agregados; não é um benchmark de capacidade operacional.

Budget padrão de 600 s, configurável até 3.600 s, por transferência/listagem e
por sequência de sealing, verificações de cancelamento e
timeout do subprocesso age. Uma chamada injetada bloqueada não é interrompida
por esse budget: o futuro adapter terá de impor timeouts próprios. Os testes
de subprocessos e rede POSIX reais continuam necessários.

## Segurança, interrupções e completion

Cada operação usa prefixo exclusivo `backups/<backup_id>/`. Listagem prévia
rejeita qualquer histórico nesse prefixo, inclusive delete markers. PUT e
CompleteMultipart usam condição equivalente a `If-None-Match: *`; depois,
verifica-se histórico exato e versão específica. Essa condição isolada permitiria
escrita quando houvesse delete marker corrente. Não há garantia WORM contra
administradores ou policies futuras; nenhuma retenção ou Object Lock é alterada.

Somente upload de parte com código transitório conhecido admite retry limitado,
com mesmo UploadId/número/bytes. PUT e CompleteMultipart com resposta perdida,
409/412 ou erro não são repetidos cegamente. UploadId conhecido é registrado antes
das partes; abort trata apenas esse upload incompleto. Falha de abort ou perda
da resposta de criação pode deixar partes cobradas: preservar journal e exigir
diagnóstico, sem descoberta/limpeza de uploads alheios ou lifecycle automático.

Confere-se tamanho e SHA-256 após GET da **versão exata**, sem usar ETag como
SHA-256. Todas as versões de artefatos, objetos de dados e delete markers
referenciados são verificadas novamente antes do marker. Completion é a última
escrita remota e contém somente o metadata permitido pelo contrato. O manifesto
e o inventário são cifrados. Falha após escrita do marker pode deixá-lo no
simulador enquanto o journal permanece incompleto: não assumir sucesso nem
retomar automaticamente. O marker não é assinatura nem autorização de Destroy.
`upload` rejeita completion diretamente. `publish_completion` exige conjunto
documental validado e seis versões de artefatos/manifesto verificadas, com os
mesmos descritores do manifesto. Qualquer ambiguidade em upload bloqueia a sessão.
Alterações do receipt durante sealing abortam antes da publicação do marker.

Intents/journal usam o Store do PR3: gravação atômica, fsync e lock local.
Arquivos sensíveis têm modo 0600 e staging 0700 em POSIX. SafeRoot rejeita
traversal, reparse/symlinks, hardlinks e arquivos especiais. Assinaturas de arquivo
e hash vinculam plaintext à captura; alterações detectadas abortam. Arquivos
parciais são preservados para diagnóstico, sem limpeza automática de dados.
No Windows os testes usam diretórios temporários do usuário; chmod não comprova
ACL Windows. Não usar esse staging para segredos operacionais.

## age e custódia

Somente age v1.3.2, destinatário público nativo X25519 `age1...`, sem plugins,
SSH recipients, passphrase ou decryption no produto. Executável com SHA-256
esperado e versão fixada, subprocesso sem shell, plaintext por stdin e ciphertext
por descritor de arquivo exclusivo; stderr descartado e environment reduzido.
Não há senha/chave privada em argumentos. Hash esperado deve vir da cadeia de
procedência verificada, não de binário arbitrário. Diretório da ferramenta deve
ser protegido contra escrita por outros usuários; host comprometido não é coberto.
O envelope recebe uma verificação limitada de framing, não validação de MAC/AEAD
ou prova de autoria. Somente os testes de descriptografia verificam a autenticação
criptográfica usando identidades descartáveis. O transporte impede plaintext
óbvio, mas depende do produtor age fixado para criptografia válida.

O `recipient_id` é identificador de custódia, não chave nem prova de que existe
uma chave privada recuperável. Operador deverá guardar a chave definitiva fora
de EC2/runner/Git, com procedimento separado de recuperação e teste isolado.
Não foi criada chave definitiva. Somente testes geram identidades descartáveis
em diretórios temporários fora do repositório e removem essas fixtures ao terminar.

## Executar testes locais

Usar dependências do PR2 já disponíveis e binários oficiais previamente aprovados.
Não há download/instalação automática nos testes ou módulos.

```powershell
$env:PR5_TEST_AGE = 'CAMINHO_LOCAL_IGNORADO/age.exe'
$env:PR5_TEST_AGE_KEYGEN = 'CAMINHO_LOCAL_IGNORADO/age-keygen.exe'
python -m unittest discover -s scripts/backup/sealing/tests -p 'test_*.py' -v
```

Sem age, os testes criptográficos são pulados com motivo explícito e não há prova
de criptografia. Com age, roundtrip, corrupção, truncamento e chave incorreta são
testes criptográficos reais com dados fictícios; não são restauração Nextcloud.
Os ciphertexts sintéticos do teste de transporte não constituem prova criptográfica.

Leia [o relatório PR5](../../../docs/phase-7-pr5.md) e
[o contrato PR1](../../../docs/phase-7.md). Cinco skips POSIX anteriores e ensaios
Linux PostgreSQL/Nextcloud, SIGKILL, reboot e escritores externos permanecem
bloqueadores. Integridade de um conjunto não comprova recuperação funcional.
