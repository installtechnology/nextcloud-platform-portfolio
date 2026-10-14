# Fixture S3/IMDS — auditoria e preparação local

Data: 2026-10-10. Base: preparação Linux `cb86ac7`, checkout inicialmente limpo.
Os cinco testes POSIX foram aceitos pelo operador, execução GitHub Actions
38028641556. Isso não homologa captura, backup ou restauração.

## Histórico da primeira auditoria: candidato5.1.17

**Na primeira auditoria, implementação suspensa antes de criar Compose/stub.** Foi encontrada incompatibilidade concreta na paginação do Moto5.1.17.
Qualquer mudança de versão/emulador ou correção de comportamento exige decisão
do operador. Nenhum schema, guard ou código dos PR1–PR6 foi alterado.

## Evidências e limites

Foram examinados o desenho aprovado, Compose/instalador/storage guard, backend
PR3, journal/captura, inventário/hooks PR5, harness/orquestração PR6 e contrato PR1.
Consultados somente fontes e manifestos públicos oficiais. Sem imagens/camadas,
pacotes, VM, Docker, AWS, dados operacionais, manutenção ou execução de workflows.
Fontes e reproducer auxiliar ficam em `.validation-tools`, ignorado pelo Git;
não são configuração operacional nem novo componente da fixture.

### Nextcloud / credenciais / TLS

O [lock oficial 3rdparty v34.0.4](https://raw.githubusercontent.com/nextcloud/3rdparty/v34.0.4/composer.lock)
fixa aws/aws-sdk-php 3.376.2, Guzzle7.15.2, promises2.5.1 e psr7 2.13.0.
O SDK tem source ref ff2f61f280fc46081a1fbb7ae201a09f431d436d.
Isso comprova versões declaradas pelas fontes da release, não bytes efetivos
dentro da imagem Docker: conferir installed.json/Sdk::VERSION e arquivos no
digest Nextcloud continua gate da primeira execução isolada autorizada.

O [provider desse ref](https://raw.githubusercontent.com/aws/aws-sdk-php/ff2f61f280fc46081a1fbb7ae201a09f431d436d/src/Credentials/InstanceProfileProvider.php)
aceita endpoint HTTPS configurado por AWS_EC2_METADATA_SERVICE_ENDPOINT e resolve
AWS_EC2_METADATA_V1_DISABLED. O default provider, usado pelo Nextcloud sem shared
config files, ainda tenta env/web identity e ECS antes de IMDS conforme configuração.
Logo é necessário rejeitar credenciais estáticas, profiles, web identity, ECS e
proxies no sandbox, não apenas deixar key/secret ausentes no objectstore.

O stub proposto atende token PUT e role/credenciais GET com IMDSv2, TTL e dados
fictícios, sem upstream. Endpoint literal HTTPS, DNS privado e CA sintética são
compatíveis com o código estudado. A ausência de resposta não muda o endpoint
resolvido para o IMDS real; bloquear link-local IPv4/IPv6 e rotas externas na VM
continua obrigatório, inclusive para fontes de credenciais alternativas.

O [S3ConnectionTrait v34.0.4](https://raw.githubusercontent.com/nextcloud/server/v34.0.4/lib/private/Files/ObjectStore/S3ConnectionTrait.php)
permite hostname/port, HTTPS e virtual hosting; escolhe uma cadeia de credenciais,
sem exigir key/secret. TLS e CA pública da fixture devem ser validados para S3
e IMDS sem verify=false. Não foi executado handshake; curl.cainfo/openssl.cafile,
SAN/DNS, SDK empacotado e resposta negativa com CA/hostname incorretos permanecem
ensaios obrigatórios. Não se deduz compatibilidade runtime de leitura das fontes.

### Moto / manifestos

GHCR confirma tag5.1.17, índice:
sha256:0361ac8f0cc6687dcf3835da1d60e31cf457fb0d49229d56586f2aa5c510f170.
Há descritores LinuxAMD64 e LinuxARM64, além de descritores unknown/unknown que
não são plataformas executáveis aprovadas. Fixture aprovada continua AMD64:
sha256:1f9ebe4431bddafd6fbca49d5244a08f180635cf11e9e7c8373bf8b5407ba2bc.
Hash dos bytes de manifestos coincide com Docker-Content-Digest. Camadas/config
não foram baixadas, assinatura e vulnerabilidades não verificadas.

As [APIs documentadas](https://docs.getmoto.org/en/5.1.17/docs/services/s3.html)
incluem versões, delete markers e listagens, mas presença da API não comprova
semântica equivalente à AWS. Moto server aceita cert/key para TLS. A imagem
e seu runtime Python/TLS/non-root/read-only ainda não foram executados.

## Bloqueador: IsLatest incorreto em paginação

Fonte: [S3Backend.list_object_versions 5.1.17](https://raw.githubusercontent.com/getmoto/moto/5.1.17/moto/s3/models.py),
linhas 1985–2105; a resposta XML serializa is_latest diretamente em
[responses.py](https://raw.githubusercontent.com/getmoto/moto/5.1.17/moto/s3/responses.py).

Cada chamada começa com `last_name=None`. O código pula versões anteriores até
encontrar KeyMarker/VersionIdMarker, mas não atualiza last_name enquanto pula.
A primeira versão retornada na próxima página acaba marcada is_latest=true,
mesmo sendo uma versão antiga da mesma chave da página anterior.

Exemplo sintético, MaxKeys=1, timestamps distintos:

| Página | Key | VersionId | IsLatest retornado |
| --- | --- | --- | --- |
| 1 | synthetic.txt | new | true |
| 2 | synthetic.txt | old | true, incorreto |

O mesmo ocorre com delete marker corrente na página1 e objeto antigo na página2.
Também pode ocorrer no limite padrão quando mais de 1000 versões de uma chave
atravessam páginas; limitar o dataset não é conserto da semântica.

O PR5 exige exatamente um registro IsLatest por chave no conjunto paginado.
Ele rejeita esses dados com **AMBIGUOUS_CURRENT_VERSION**, antes de HeadObject
ou promoção do inventário. Essa rejeição é correta e permanece intacta.
Não omitir versões, truncar histórico, reinterpretar os flags silenciosamente,
alterar schemas ou aceitar dois registros correntes para fazer o teste passar.

### Reprodução local executada

Foi extraída por AST a função oficial, sem editar seu corpo, e executada com
objetos Python sintéticos e stdlib. Nenhum import de Moto, servidor, socket,
Docker ou SDK externo. O adaptador de resposta do teste apenas projeta os campos
que responses.py serializa; não corrige IsLatest.

Quatro testes passaram:

1. Controle de uma página: exatamente uma versão corrente.
2. Duas páginas de versões da mesma chave: reproduz duas flags correntes.
3. Delete marker/versão antiga separados por página: reproduz a ambiguidade.
4. Inventário PR5 rejeita a projeção pelo código exato AMBIGUOUS_CURRENT_VERSION.

Hash SHA-256 de models.py lido:
f6ae871087e181bc314b033462fae2d9d32a0b50d2074addfe410e95ae7c3595.
De responses.py:
8e86f78699d23f0eccf7d66981fd2eee3ee043dc9f73d219a6975432dfc89235.
Esses hashes identificam fontes analisadas; não autenticam camadas da imagem.
É evidência de comportamento da função no nível de fonte com dependências
sintéticas, **não execução da imagem Moto nem teste HTTP/S3 real**.

## Alternativas para decisão

1. **Preferida:** auditar uma release oficial Moto com correção comprovada,
   reaplicar o reproducer e testes de paginação/delete markers, escolher novo
   digest/manifesto/procedência e obter aprovação antes da implementação/download.
   Nenhuma release substituta foi selecionada ou presumida corrigida.
2. Manter5.1.17 só para subensaios S3/IMDS/PG, registrando paginação como não
   homologada e esperando correção para a integração ao inventário. Não satisfaz
   homologação completa nem libera captura operacional; exige revisão do escopo.
3. Patch explícito no emulador de teste ou outro emulador: precisa revisão de
   fonte, cadeia de build/dependências, novos digests e ensaios. Não trocar por
   wrapper que mascare a resposta defeituosa do Moto. Não implementado.

## Ensaios posteriores preservados

Após decisão, preparar a allowlist de cinco serviços, redes internal, ausência
de ports/host networking/mounts operacionais, imagem Redis também conferida,
endpoints literais, credenciais somente fictícias e guard de origem/exclusividade.
O DockerBackend padrão permanece limitado à fixture PR3 de três serviços e
recusa env AWS_*; a fixture ampliada precisa adapter separado.

Sequência futura: handshake positivo/negativo TLS e IMDSv2; inicialização/versionamento
S3 sintéticos; instalação nova NC via instalador/guard existentes; upload/download;
paginação/VersionIds/delete markers; captura PG custom/config/identity; escritores
concorrentes/cron/sessões; manutenção original on/off; journal/lock, SIGTERM,
SIGKILL/exec órfão, ENOSPC, reboot/systemd e restore em destino independente.
Todos permanecem **não executados**, sem transformação em completion operacional.
Igualdade de inventários não substitui fencing/drenagem contínuos.

Dependências da primeira execução: VM Ubuntu24.04 descartável/Docker/systemd,
imagens verificadas e autorizadas, geração TLS/secrets sintéticos protegidos,
firewall/egress/link-local bloqueados e executor/adapter revistos. Nenhuma dessas
dependências foi instalada ou ativada. Nenhum primeiro ensaio foi autorizado.

Somente documentação local nesta entrega; sem commit/push. A decisão Moto bloqueia
a implementação. Sucesso POSIX não implica backup completo, restore ou Destroy.

## Validações da primeira auditoria (histórico)

- Reproducer da função Moto: 4 aprovados, zero skips.
- PR6/preparação: 146 aprovados, zero skips.
- Contratos PR1: 73 aprovados, zero skips.
- PR3: 71 aprovados e 3 skips POSIX no Windows (74 descobertos). Os skips desta
  execução Windows não desfazem o aceite Linux anterior nem são ensaios Docker.
- Inventário PR5: 29 aprovados, zero skips.
- Sintaxe do reproducer, links locais, scan de secrets e diff-check: aprovados.
- Git confirma somente estes dois documentos alterados/criados, staging vazio;
  scripts, Terraform/IAM, Ansible, Docker, workflows e contratos inalterados.

Não executados: imagem Moto, handshakes TLS/IMDS, Nextcloud/PG/Redis reais,
containers e ensaios de falhas da fixture. Não foi criado executor, Compose,
stub, teste de integração real ou mecanismo de aprovação de completion.


## Decisão e resultado atual: candidato5.1.22

O operador autorizou5.1.22. A fonte oficial
[models.py5.1.22](https://raw.githubusercontent.com/getmoto/moto/5.1.22/moto/s3/models.py)
calcula is_latest/last_name antes de pular os marcadores de paginação.
SHA-256 da fonte: b25f031c5e785212801af5aec1b8d1732736478a424c59a0383f5f1e52a20175.
O mesmo reproducer de duas páginas agora retorna true/false tanto para objeto
corrente/antigo quanto para delete marker corrente/objeto antigo.

Sete testes da função oficial ampliam a reprodução: limites1/2/3/4/1000,
1001versões atravessando o limite, múltiplas chaves Unicode, IDs opacos, delete
marker antigo e aceitação PR5. Respostas deliberadamente inválidas continuam
rejeitadas por AMBIGUOUS_CURRENT_VERSION (corrente ausente/duplicado) e
INCOMPLETE_LISTING (marcador de paginação incompleto). Nenhum código PR5 mudou.
A projeção apenas serializa campos, sem corrigir flags ou omitir versões.

[Release oficial](https://github.com/getmoto/moto/releases/tag/5.1.22).
Manifesto consultado em ghcr.io/getmoto/motoserver:5.1.22:

- Índice: sha256:1e3802c95726373544967b428201c548f0247c15b00db2d96a5ba0a77d8643b8.
- LinuxAMD64: sha256:117238c6e7e3b566387c4c74e6fb0b6fdc34a094920c2154cf06f11dc72d9f37.
- Há também LinuxARM64 e descritores de atestação unknown/unknown.
- SHA dos bytes do manifesto coincide com Docker-Content-Digest. **Não foram
  baixadas/configuradas/executadas camadas nem verificada assinatura**.

Os pins operacionais Nextcloud/PG/Redis continuam acessíveis, todos com descritor
AMD64. Redis/PG ainda correspondem às tags consultadas. A tag Nextcloud34.0.4-apache
agora aponta para sha256:0e5688ba0988b9112cf03f9c6bab53eccb466d278435f6596f16c1f25acacf74;
o pin aprovado72f5cc… foi preservado. Conferir versão/SDK/bytes desse pin na
primeira execução autorizada; nenhum upgrade ou download foi feito.

Foi identificado que bucket_empty no instalador original usa endpoint AWS
padrão. O wrapper **exclusivo de sandbox** reutiliza o adapter e algoritmo original,
substituindo só essa leitura por S3Client com endpoint HTTPS fixo e CA explícita.
Guards de banco vazio/local data/storage, lock e confirmação continuam originais.
O instalador e Compose operacionais não foram alterados.

A [preparação local](../scripts/backup/sandbox/README.md) inclui cinco serviços,
redes internas, secrets por arquivos ainda inexistentes, TLS validado configurado,
IMDSv2 fictício sem upstream, preflight SDK e verificações de modelos injetados.
Não inclui executor Docker, coletor/attestor real, adapter de captura, geração de
certificados ou inicialização de bucket. Estes permanecem bloqueadores explícitos.
Não representa homologação runtime nem conclusão da cadeia de backup.


Dependências declaradas pela fonte oficial5.1.22: Python>=3.9,
boto3>=1.9.201, botocore>=1.20.88 com exclusões1.35.45/46,
cryptography>=35, werkzeug>=0.5 com exclusões2.2.0/1, requests>=2.5.
O [Dockerfile oficial](https://github.com/getmoto/moto/blob/5.1.22/Dockerfile)
parte de python:3.13-alpine, instala .[server] e curl. Essas dependências não
estão fixadas por versões na fonte de build; o digest da imagem fixa o resultado
publicado, cuja composição/SBOM ainda não foi inspecionada. Nenhum pip install
ou build foi executado. O stub usa apenas stdlib; não requer pacote adicional.


## Validações atuais e escopo da entrega

| Suíte local Windows | Resultado |
| --- | --- |
| Fixture (41 originais + 18 regressões de auditoria; 7 testes da fonte Moto incluídos) | 59 aprovados, zero skips |
| PR1 | 73 aprovados |
| PR2 | 187 aprovados, 2 skips POSIX |
| PR3 | 71 aprovados, 3 skips POSIX |
| PR5 | 144 aprovados |
| Inventário PR5, também incluído na suíte acima | 29 aprovados |
| IAM semântico com plano Terraform mockado previamente obtido | 21 aprovados |
| PR6/preparação Linux | 146 aprovados |
| Storage guard, com configs oficiais em cache | 17 aprovados |
| Instalação inicial, adapters sintéticos PHP | 18 aprovados |
| Controlador/deploy offline | 13 aprovados |

PHP lint dos três arquivos PHP novos, sintaxe shell sem execução, AST Python,
JSON, verificações estáticas da stack, links locais, Gitleaks e diff-check incluindo
untracked: aprovados. Link da documentação Moto5.1.22 retornou HTTP403 no acesso
automatizado; não foi possível validar essa página por HTTP. Fontes oficiais da
tag, release e manifestos foram consultados independentemente.

Os cinco skips Windows correspondem aos testes POSIX já aceitos na execução
Linux38028641556, não a novos testes homologados aqui. Nenhum teste Docker,
TLS handshake ou ensaio de captura/restore real foi executado. A suíte IAM usa
mock offline existente; não consulta IAM nem prova autorização AWS.

Arquivos desta preparação (19, incluindo os dois documentos pendentes):

```text
docs/phase-7-s3-imds-fixture-design.md
docs/phase-7-s3-imds-compatibility-audit.md
scripts/backup/sandbox/README.md
scripts/backup/sandbox/__init__.py
scripts/backup/sandbox/__main__.py
scripts/backup/sandbox/compose.json
scripts/backup/sandbox/imds.py
scripts/backup/sandbox/install-sandbox.php
scripts/backup/sandbox/moto_server.py
scripts/backup/sandbox/platform.config.php
scripts/backup/sandbox/sandbox-preflight.php
scripts/backup/sandbox/sandbox-preflight.sh
scripts/backup/sandbox/spec.py
scripts/backup/sandbox/tests/test_imds.py
scripts/backup/sandbox/tests/test_moto_source.py
scripts/backup/sandbox/tests/test_safety.py
scripts/backup/sandbox/trial-plan.json
scripts/backup/sandbox/trust.ini
scripts/backup/sandbox/validation.py
```

HEAD e origin/main local continuam cb86ac71f58285bc897a8b385483e8468590c513,
staging vazio. Uma documentação tracked modificada e dezoito untracked; sem
commit/push. A referência remota local não substitui fetch; nenhuma nova consulta
GitHub do estado remoto foi necessária nesta preparação.
Terraform, IAM, Ansible, Compose/deploy operacional, workflows, scripts PR2–PR6
existentes e contratos normativos intactos. Fontes/reproducer auxiliar/relatórios
ficam ignorados em .validation-tools. Nenhuma instalação/download de camadas,
AWS/EC2/S3 real/IMDS real, manutenção, workflow, captura ou restore realizado.
A EC2 não foi acessada nem iniciada; seu estado operacional não foi consultado.


## Auditoria final dos 19 arquivos

Problemas concretos corrigidos exclusivamente na fixture:

- Snapshot recusava host networking, mas ainda aceitava namespace container:* e
  DNS/ExtraHosts sobrescritos. Agora rejeita ambos e VolumesFrom.
- Marker de contexto e project ID passaram a ser obrigatórios no preflight PHP,
  config e servidores. Placeholder é permitido só no modelo Compose; não em
  snapshot runtime. Nenhum fallback de produção foi criado.
- Root/binds/volumes foram restringidos ao namespace do sandbox, fora do checkout
  e de caminhos sensíveis. Binds exigem arquivo regular RO, origem/destino exatos,
  sem symlinks; volumes locais rotulados têm nome e destino vinculados ao projeto.
  Tmpfs extra, inclusive sobre credenciais, é rejeitado.
- UID1000/read-only/CapDrop ALL/NNP dos auxiliares agora são verificados também nos
  snapshots. Proxies e overrides Moto adicionais são rejeitados pelos servidores.
- Entradas malformadas produzem códigos fixos sanitizados, sem rastros/paths;
  testes IMDS evitam mostrar tokens até em mensagens de falha.

Wrapper: SDK falso e factory sintética PHP, algoritmo original sem alterações,
confirmam que todos os callbacks preservam identidade exceto bucket_empty.
Consulta continua MaxKeys=1/bucket fixture-data; endpoint HTTPS e CA ficam fixos.
Banco parcial, bucket não vazio e exceção simulada TLS/SDK abortam sem instalar;
installed=true não consulta S3 nem reinstala. Não é ensaio com SDK/containers reais.

Nenhuma dependência operacional foi adicionada ao deploy/captura padrão. Moto é
importado apenas dentro de seu entrypoint explícito de sandbox; nenhuma imagem,
pacote ou dependência foi instalada nesta auditoria. Fixtures/fixtures SDK não
foram convertidas em evidência operacional. Runtime_allowed/backup_complete
permanecem false; AMBIGUOUS_CURRENT_VERSION continua intacto no PR5.

Não apareceu incompatibilidade que exigisse alterar a arquitetura ou controles
operacionais existentes. Esta avaliação permite somente propor publicação
preparatória. Execução exige launcher/preflight e coletor/attestor independentes,
VM/Docker/systemd exclusivos, imagens/SDK conferidos, CA/secrets sintéticos
protegidos, egress/link-local negados, inicialização S3 somente fictícia e ensaios
TLS/fencing/captura/falhas/restore autorizados separadamente.

O preflight também rejeita HOME alternativo do Nextcloud e o modelo rejeita
PYTHONPATH/PYTHONHOME, LD_PRELOAD/LD_LIBRARY_PATH e hooks de shell por ambiente.
Isso evita fontes de credenciais ou loaders alternativos selecionados por override.
Os 59 testes passaram sem skips, incluindo os 41 originais.
