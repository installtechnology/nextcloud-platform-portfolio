# Fase 7 — PR5: criptografia e transporte preparados localmente

Base: PR4 `1f0a629`. Implementação local com decisões de age e hooks autorizadas.
Sem AWS, EC2, buckets reais, IAM, Terraform, deploy ou workflow de backup. PR6
não iniciado. Nenhum contrato normativo foi alterado. Publicação autorizada
exclusivamente como implementação preparatória, sem homologação operacional.

## Arquitetura e compatibilidade

[Módulos e execução](../scripts/backup/sealing/README.md) detalham inventário,
projeção, age, transporte, journal, verificação e completion. Os quatro schemas
existentes representam os documentos sem alteração. O receipt da projeção é
interno: armazena VersionIds dos delete markers sem adicionar campos normativos.

Captura PR3 não recebia inventário dentro da janela quiescente. Hooks opcionais
agora observam antes/depois do dump/configs, antes de restaurar serviços, e
reconfirmam o backend quiescente. O argumento padrão é None; CLI/Ansible/deploy
existentes não ativam hooks. Falhas seguem os intents/ownership de manutenção do
PR3. Não desativar manutenção preexistente ou sem ownership comprovado.

Receipts devem vincular source, instalação, operação, consistência e fence,
ordenar etapas e declarar controles de todos os escritores. Ausência, mudança
ou falha aborta, mesmo com inventários iguais. Só existe attestor sintético;
nenhuma flag produz prova de fencing real. Conclusão simulada exige Store
sintético e API simulada, retorna `backup_complete=false` e mantém o journal PR3
`local_candidate_only`. Manifesto/marker v1 produzidos nos testes são documentos
de ensaio, não backups operacionais nem autorização para Destroy.

A captura permanece compatível com os caminhos reais definidos no PR3: dump
custom PostgreSQL 17, configuração completa, HTML mutável e parâmetros de
recuperação. A imagem Compose continua Nextcloud 34.0.4 e PostgreSQL
17.11-bookworm, com digests preservados. Nenhum dump/instalação real foi feito.
Catalog check e identidade dependem do produtor PR3; mocks não comprovam conteúdo
recuperável de um dump ou completude operacional dos arquivos.

## Auditoria final: problemas corrigidos

- Receipts não registravam os instantes das duas observações. Agora conservam
  início/fim UTC e rejeitam datas ausentes, inválidas, invertidas, posteriores à
  captura ou fora da janela. A sequência do journal não pode conter fases críticas
  duplicadas. Esses horários são internos; nenhum schema PR1 foi alterado.
- O backend não era reconfirmado imediatamente após o hook inicial. Essa
  reconfirmação agora impede iniciar o dump se o hook reabrir um escritor.
  Hooks de operações distintas são rejeitados. O fluxo sem hooks permanece igual.
- Os identificadores de database, owner e table prefix declarados agora precisam
  corresponder ao baseline PR3, além da identidade e hashes dos quatro artefatos.
  Isso não inspeciona semanticamente um dump real ou o conteúdo dos archives.
- A segunda leitura do receipt não era comparada à primeira. Uma alteração,
  mesmo em um receipt ainda válido, agora impede o completion marker.
- O transporte permitia chamar upload diretamente para completion. Há agora um
  gate separado: valida schemas/semântica e vincula os cinco artefatos e o manifesto
  às versões, buckets, keys, tamanhos e hashes efetivamente verificados. O gate
  rejeita conjuntos incompletos e não constitui autorização operacional.
- Uma falha de transferência deixava a sessão aberta para outras escritas.
  Agora o journal registra `blocked=true`; o diagnóstico explícito continua sendo
  somente leitura, sem retomar, apagar ou promover o conjunto.
- Listagens agrupadas por delimiter/CommonPrefixes, páginas truncadas vazias,
  ordem regressiva entre páginas, continuação contraditória e keys acima de
  1.024 bytes UTF-8 são rejeitadas. VersionIds continuam opacos; não se presume
  que possam ser ordenados ou que um cursor prove a completude de um snapshot.
- Respostas PUT/multipart malformadas ou com campo Error não são aceitas como
  sucesso. Resposta perdida continua ambígua; não há retry cego.
- Uma saída age contendo somente a assinatura textual podia passar pela
  conferência inicial. A verificação de framing agora é limitada e exige stanza
  X25519, footer e espaço para nonce/tag. **Não verifica MAC/AEAD com chave pública**.
  A autenticação criptográfica é testada por descriptografia com identidade fictícia;
  não comprova origem/autoria do backup. O transporte também rejeita arquivos
  claramente plaintext antes de qualquer PUT; essa barreira não autentica ciphertext.
- O simulador agora limita memória/tipos também na entrada de partes multipart.

As correções são locais e restritas aos mesmos 19 arquivos. Não alteram fencing
operacional, schemas, IAM, Terraform, workflow ou deploy. Inventários iguais e
flags de simulador nunca substituem evidência real de controles de escritores.

## Procedência do age autorizado

Pacote portátil Windows AMD64 obtido da
[release oficial v1.3.2](https://github.com/FiloSottile/age/releases/tag/v1.3.2).
SHA-256 do ZIP comparado **antes da execução** ao campo digest da API oficial
GitHub da release:

```text
f48d8f8f9ebe903ab5027ed067652f2cc1db94bc206976430133b905dcd8e8c7
```

Arquivo e registro de procedência ficam em diretório ignorado `.validation-tools`.
Não há instalação global ou mudança de PATH. Somente age.exe e age-keygen.exe
foram executados, ambos retornando v1.3.2; plugins incluídos no pacote não foram
executados. Nenhum instalador ou script remoto foi usado.

O digest foi obtido por HTTPS de infraestrutura GitHub oficial. Essa verificação
é mais forte que comparar somente um hash calculado após download, mas confia na
mesma infraestrutura da distribuição. **Sigsum não foi verificado**; não alegar
autenticação independente do mantenedor. Nenhuma dependência adicional de
verificação foi instalada. Custódia definitiva ainda precisa de aprovação.
[Especificação age](https://c2sp.org/age).

## S3 e IAM: limites do ensaio

Protocolo usa somente operações cobertas pelo PR4: listagem de versões e HEAD/GET
de dados, PUT/GET/versões e multipart/abort de backups. Sem delete, state, SG,
SSM, EC2 write ou administração IAM. Role PR4 continua opt-in desabilitada;
publicação/código não concede permissões. Não há adapter AWS SDK neste PR.

If-None-Match não impede escrita sobre chave cujo corrente seja delete marker.
Histórico, ID único, reserva local e verificação da versão completam o controle
do produtor; não oferecem WORM. CompleteMultipart com resposta perdida/409 não
recebe retry cego. O futuro adapter deverá tratar também HTTP 200 com erro no
corpo, owner/TLS/timeouts e ausência de drift nas policies.
[Condições S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html),
[CompleteMultipartUpload](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CompleteMultipartUpload.html).

Backup v1 depende da preservação do bucket de dados e das versões referenciadas.
Não há cópia independente dos dados, proteção contra perda total desse bucket,
expiração automática, retenção nova ou Object Lock. Restauro funcional e qualquer
autorização de Destroy continuam fora do PR5.

## Custos futuros e interrupções

Nenhum custo AWS foi gerado nesta implementação. Uso futuro acrescentará bytes
cifrados e versões no bucket de backups, requests LIST/HEAD/PUT/GET/multipart e
capacidade/disco temporário para plaintext, ciphertext e journal. Inventários
duplos e verificação por download consomem requests e leitura; avaliar transferência
entre regiões/serviços antes de operar. Recapturas preservam conjuntos anteriores.
Uploads multipart órfãos podem gerar armazenamento cobrado até decisão explícita,
sem lifecycle de limpeza criado aqui. Dimensionamento e tempo de manutenção
precisam de medição em Linux isolado antes de usar dados reais.

## Validação e pendências

Validação Windows: **144 testes PR5 aprovados** (114 originais e 30 regressões
adicionadas nesta auditoria), sem skips criptográficos: age
real, identidades descartáveis, roundtrip com múltiplos chunks, corrupção,
truncamento e destinatário incorreto. Transporte/controle de escritores usam
simuladores; alteração de assinatura de plaintext é injeção sintética, não ensaio
POSIX concorrente. Testes cobrem paginação,
VersionIds/delete markers, checksums, fencing insuficiente, manutenção, hooks
interrompidos, criptografia real fictícia, corrupção, conflito histórico,
multipart/retries/cancelamento, respostas ambíguas e marker prematuro.
Os negativos conferem reason codes específicos, não apenas a ocorrência de erro.

Regressões: PR1 **73**; PR2 **187 aprovados / 2 skips**; PR3 **71 aprovados /
3 skips**; controlador **13**; storage guard **17**; instalação inicial **18**.
Testes semânticos IAM **21**, utilizando apenas saída de mocks já disponível,
sem Terraform plan real ou consulta AWS. `terraform validate` e `terraform test`
reexecutados em cópias locais verificadas contra o código, sem backend remoto,
tfvars privados ou provider de rede: **19 testes bootstrap, 2 persistent e 5
runtime aprovados**, todos com mocks. Syntax AST, links locais, gitleaks e
diff-check incluem os arquivos novos. `terraform fmt -check -recursive terraform`
também aprovado; nenhum root foi modificado, sem init/plan/apply operacional.

O scanner inicialmente classificou um digest público do executável de geração
como generic-api-key. O digest foi identificado como constante SHA-256 pública,
sem alterar seu valor ou adicionar bypass/allowlist ao scanner. Nenhuma
credencial real foi encontrada. Chaves privadas fictícias não ficam em fontes,
scan snapshots, saídas ou logs.

A execução no sandbox Windows restrito falhou em handles nativos de SafeRoot.
O ensaio foi reexecutado com acesso local autorizado a esses handles, preservando
as proteções. Não interpretar a falha ambiental como teste POSIX ou relaxar SafeRoot.
O SHA-256 do ZIP armazenado foi novamente conferido com o registro do digest
publicado; isso não acrescenta uma assinatura Sigsum à cadeia de procedência.
A consulta local ao WSL não listou distribuições instaladas; Docker/Ansible não
estão disponíveis para ensaios isolados nesta auditoria. Nada foi instalado.

Os cinco skips restantes são: PR2 — truncamento concorrente real e FIFO/socket;
PR3 — ambiente sem credenciais herdadas, streaming de subprocesso e timeout real
do subprocesso POSIX. Testes Windows e simuladores não substituem esses ensaios.

## Arquivos do PR5

Escopo aprovado para publicação: dois arquivos existentes modificados e dezessete novos:

```text
docs/README.md                                      (modificado)
docs/phase-7-pr5.md                                 (novo)
scripts/backup/capture/engine.py                    (modificado)
scripts/backup/sealing/README.md                    (novo)
scripts/backup/sealing/__init__.py                  (novo)
scripts/backup/sealing/common.py                    (novo)
scripts/backup/sealing/completion.py                (novo)
scripts/backup/sealing/crypto.py                    (novo)
scripts/backup/sealing/hooks.py                     (novo)
scripts/backup/sealing/inventory.py                 (novo)
scripts/backup/sealing/simulator.py                 (novo)
scripts/backup/sealing/transport.py                 (novo)
scripts/backup/sealing/tests/support.py             (novo)
scripts/backup/sealing/tests/test_completion.py     (novo)
scripts/backup/sealing/tests/test_crypto.py         (novo)
scripts/backup/sealing/tests/test_hooks.py          (novo)
scripts/backup/sealing/tests/test_inventory.py      (novo)
scripts/backup/sealing/tests/test_security.py       (novo)
scripts/backup/sealing/tests/test_transport.py      (novo)
```

Windows não comprova fsync POSIX, sinais de subprocessos, reboot ou fencing real.
Os **2 skips PR2 e 3 skips PR3** permanecem bloqueadores; não foram removidos.
Ainda necessários: Linux descartável sem credenciais/rede operacional; testes
POSIX existentes; ensaio PostgreSQL/Nextcloud reais com dados fictícios; drenagem,
cron/CLI/escritores externos, SIGKILL/reboot, recuperação de journal e custódia.
Só depois e com autorização própria avaliar APIs/IAM/OIDC reais. Um roundtrip
age ou checksum válido não é restauração funcional Nextcloud.
