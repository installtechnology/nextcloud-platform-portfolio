# Fixture S3/IMDS — desenho e preparação local sem execução

Atualização 2026-10-10: Moto5.1.22 substitui o candidato5.1.17 após decisão
explícita e reprodução da correção IsLatest no nível de fonte. Preparação local
em [sandbox](../scripts/backup/sandbox/README.md); nenhum container foi executado.
[A auditoria](phase-7-s3-imds-compatibility-audit.md) distingue evidência de fonte,
manifestos e ensaios runtime ainda pendentes. O desenho não autoriza execução,
download de imagens ou captura operacional. [Plano Linux](phase-7-linux-qualification.md).

## Dependências propostas e procedência

S3: Moto Server **5.1.22**, imagem publicada pelo projeto getmoto:

```text
ghcr.io/getmoto/motoserver:5.1.22@sha256:117238c6e7e3b566387c4c74e6fb0b6fdc34a094920c2154cf06f11dc72d9f37
```

Esse é o manifesto LinuxAMD64. O índice publicado para a tag tem digest
sha256:1e3802c95726373544967b428201c548f0247c15b00db2d96a5ba0a77d8643b8.
Consultados apenas manifestos da API pública GHCR; SHA-256 dos bytes dos
manifestos coincide com Docker-Content-Digest. **Nenhuma camada foi baixada**,
imagem executada, assinatura de publisher verificada ou scan de vulnerabilidades
realizado. Verificar config/layers/plataforma antes de uso; pin não equivale a
atestado de segurança. Não usar latest ou atualizar automaticamente a versão.
[Projeto/release](https://github.com/getmoto/moto/releases/tag/5.1.22),
[documentação S3 da versão](https://docs.getmoto.org/en/5.1.22/docs/services/s3.html).

IMDS: stub **local preparado, não executado**, protocolo de teste v1, Python stdlib, usando
a mesma imagem Moto fixada como runtime para evitar nova imagem/dependência.
Conferir executável Python dessa imagem durante o preflight isolado; se ausente,
parar e revisar, sem pip install ou imagem alternativa. O código está no sandbox, com testes do protocolo em memória; conferir hash
do arquivo revisto ao montar o futuro bundle. Não há handshake runtime aprovado. Ele não é um simulador fiel de IAM da AWS.

Nextcloud 34.0.4 Apache, PostgreSQL 17.11 Bookworm e Redis 7.4.8 Bookworm usam
os três digests já registrados no plano Linux e no Compose existente. O executor
deve conferir todos os três, além de Moto, comparando referência fixada, plataforma,
manifesto e image ID. O backend PR3 atual não confere Redis; corrigir somente
no adapter de sandbox aprovado, preservando regressões do comportamento padrão.

## Topologia fechada

VM Ubuntu24.04 x64 descartável, daemon Docker Unix local exclusivo, sem compartilhamento
de HOME/.aws/.ssh, checkout ignorado, volumes ou socket do computador operacional.
Precarregar imagens após autorização e bloquear egress antes de executar a fixture.
Sem bridge para VPC, sem rota operacional, sem host networking, privileged,
CAP_NET_ADMIN, Docker socket em containers ou portas publicadas.

Allowlist de exatamente cinco serviços persistentes da fixture:

| Serviço | Rede | Dados/acesso |
| --- | --- | --- |
| nextcloud | db_internal, storage_internal | nextcloud_html sintético, config/secrets próprios; escritor da aplicação |
| postgres | db_internal | postgres_data sintético; só socket/pares conhecidos |
| redis | db_internal | Cache descartável, sem volume operacional |
| s3-emulator | storage_internal | Moto/versionamento somente sintético; não acessa volumes NC/PG |
| imds-emulator | storage_internal | Credenciais fictícias apenas em RAM; não acessa volumes NC/PG |

Ambas as redes internal=true, sem portas host. Auxiliares com usuário não root,
cap_drop ALL, no-new-privileges, filesystem read-only e tmpfs privado quando
necessário; testar viabilidade na imagem em vez de presumir. Binds somente RO
de fontes públicas/certificados dentro da raiz privada de fixture. Labels
platform.capture.isolated=true + papel/execution ID, volumes locais exclusivos
rotulados; nenhum diretório/volume da stack atual.

Clientes de teste usam exec dentro do Nextcloud, evitando anexar containers
externos. Um writer extra é permitido apenas como injeção negativa controlada;
deve provocar rejeição. Inspeção deve alcançar também containers parados e todos
os consumidores das redes/volumes. A allowlist nunca aceita nomes arbitrários.

## S3 Primary Object Storage com endpoints somente de sandbox

Config exclusiva da fixture: bucket/região sintéticos, classe
\\OC\\Files\\ObjectStore\\S3, hostname s3.sandbox.test, porta 5000, use_ssl=true,
use_path_style=false e verify_bucket_exists=false. Sem key/secret/session_token
no objectstore. Criar e habilitar versionamento do bucket **somente no emulador**
antes da instalação. DNS aliases exatos para endpoint e hostname do bucket.
Nunca resolver amazonaws.com ou usar um endpoint real como fallback.

Moto pode usar TLS com certificados fornecidos; gerar CA/certificados descartáveis
somente depois de autorização, com SAN dos aliases. Injetar apenas CA pública
nos PHP ini de fixture (openssl.cafile/curl.cainfo) e SDKs de teste; chave TLS fica
somente no emulador/raiz privada. Não usar verify=false ou desativar validação TLS.
Config/marker esperados da fixture devem concordar desde antes do install.
Guard e algoritmo do instalador originais são reutilizados com a config de fixture
explicitamente classificada; wrapper sandbox troca somente o endpoint da leitura
bucket_empty para o emulador HTTPS com CA validada; nenhuma alteração ao platform.config.php operacional ou aos schemas.
[S3 Nextcloud 34.0.4](https://github.com/nextcloud/server/blob/v34.0.4/lib/private/Files/ObjectStore/S3ConnectionTrait.php).

A fonte tag34.0.4 permite hostname/port; SDK e CA efetivos dentro da imagem por
digest ainda precisam de conferência no ensaio. Se TLS, virtual hosting ou o SDK
embutido forem incompatíveis, parar; não cair em HTTP/path-style/credenciais estáticas.

## IMDSv2 fictício, sem acesso ao IMDS real

Endpoint exclusivo https://imds.sandbox.test:1338, TLS validado pela CA sintética.
O adapter separado de sandbox admite somente estas duas variáveis de controle:
AWS_EC2_METADATA_SERVICE_ENDPOINT igual ao endpoint literal da fixture e
AWS_EC2_METADATA_V1_DISABLED=true. Nunca admitir AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, arquivo de credenciais, profile real,
web identity, endpoint ECS ou proxies. O backend padrão continua recusando AWS_*.

Stub atende somente PUT /latest/api/token e GET de role/credenciais IMDSv2;
token em RAM com TTL, 401 para GET sem token/expirado, sem fallback v1. Credenciais
fictícias renováveis/expiráveis em RAM, sem validade na AWS e sem copiar campos
ou tokens para logs, argumentos, env ou config. Expor só contagens/status/expiração.
Não implementar proxy, redirects, forwarding ou qualquer cliente upstream.

Bloquear 169.254.169.254, fd00:ec2::254 e demais destinos externos nos controles
da VM/containers; verificar ausência de rotas e registrar tentativas sem conteúdo.
Hostname/token inválido ou emulador parado devem falhar, nunca acessar metadata
padrão. A configuração deve preceder a primeira inicialização e qualquer occ.
O SDK PHP atual documenta endpoint configurável, mas conferir a versão embutida
na imagem fixada é um gate, não uma compatibilidade já testada.
[Provider PHP](https://docs.aws.amazon.com/aws-sdk-php/v3/api/class-Aws.Credentials.InstanceProfileProvider.html).

## Adapter de teste, fencing e inventários

Criar adapter/comando separados e explicitamente sandbox-only, não uma flag que
ative o backend operacional. Default PR3, projeto nc-capture-test-*, root/0700,
identidade, mounts, exclusividade, quiescência e ownership continuam protegidos.
Auxiliares Moto/IMDS não serão tratados como writers NC, mas sua identidade e
isolamento serão reconfirmados. Não modificar CLI operacional, IAM ou deploy.

Antes da janela: lock/lease comum, manutenção original/fingerprint, inspeção dos
cinco serviços, fencing de clients/cron/CLI/host e sessões PostgreSQL. Impedir
novas requisições sintéticas; permitir que as já iniciadas drenem. Registrar
WINCH, saída zero e nenhuma sessão/writer persistente. Maintenance mode isolado
e poll pontual de sessões não comprovam ausência de escritores.

O attestor de teste só emite evidência depois de observar processos/cgroup,
conexões, redes e lease continuamente; mudança de ID/ownership/namespace ou writer
desconhecido invalida a janela. Root/daemon/operator não cooperativo continua
fora da garantia; VM exclusiva e controles de acesso são pressupostos explícitos.
SIGKILL/timeout deve identificar também processos docker exec vivos no daemon.
Recuperação não remove manutenção alheia nem reaproveita candidato parcial.

Integração proposta dos inventários: adapter **somente de teste** traduz APIs
HTTP Moto para o protocolo paginado PR5, preso ao endpoint literal, sem AWS SDK
default discovery. Evidence é classificada sandbox, jamais autenticada operacional.
MemoryS3 e contratos permanecem intactos. Antes/depois, dump/config/identity,
operation_id/consistency_id/instantes e fence_id pertencem à mesma janela. Duas
listagens iguais não substituem controle de escritores.

## Gates de compatibilidade do emulador

Validar Put/Get/Head, ACL private, versionamento, leituras por VersionId, delete
markers e IsLatest; chaves Unicode, paginação KeyMarker/VersionIdMarker, replay,
lista truncada e erros; upload multipart/dados corrompidos/falha de conexão.
Criar versões/delete markers somente sintéticos. Comparar IDs como strings
opacas, sem assumir ordenação temporal ou formato AWS.

Moto5.1.22 declara ListObjectVersions/versioning e outras APIs S3, mas tem
limitações: condições de bucket policy não são avaliadas integralmente;
GetObjectAttributes não retorna todos os atributos de delete markers; listagem
multipart tem parâmetros não implementados. Não usar ETag como SHA-256, nem
aprovação Moto para provar IAM/bucket policies/TLS obrigatório/locking S3 reais.
Casos não suportados devem ficar explicitamente pendentes ou ser testados via
injeção controlada, sem rotular mocks como evidência real. Não requer proxy mode.

Para falhas/reboot, Moto em memória pode perder objetos/VersionIds. Não prometer
persistência nem ressemear mantendo os mesmos IDs. Ensaio reboot do controlador
deve separar evidência local de journal/serviços da validade do conjunto S3;
se o emulador perder versões, rejeitar o conjunto. Persistência do emulador é uma
decisão posterior, caso exigida, e não justificativa para relaxar o contrato.

## Aprovação necessária

Preparação local e Moto5.1.22 autorizados. Download de imagens, VM, certificados,
executor e ensaios continuam exigindo autorização própria. Adapter de captura,
fencing e supervisão ainda pendentes; backend PR3 padrão intacto.
Recursos propostos continuam 4vCPU/8GiB/40GiB para dataset pequeno, a medir.
Sem custo AWS ou serviços Moto pagos; custo de VM local/disco/energia/tempo e
download de imagens. Nenhuma assinatura/vulnerabilidade/compatibilidade executada
é presumida. Aprovar POSIX não aprova essa fixture ou PR7 operacional.


## Atualização da auditoria final local

Pré-condições explícitas no sandbox: marker s3-imds-v1 e project ID no namespace
nc-s3-imds-test com 16 hexadecimais; snapshots rejeitam placeholders não expandidos,
DNS/extra_hosts/namespaces container:* e origem de mounts diferente do bundle.
Raiz e volumes pertencem ao mesmo projeto. Servidores rejeitam proxies e variáveis
Moto adicionais; guards PR3 continuam inalterados. A suíte local passou de 41
para 59 testes com regressões de segurança e wrapper PHP sintético.

Não há executor aprovado nem verificação real de rotas/firewall. Não invocar o
Compose bruto antes de revisar e autorizar esses componentes. Resultados de
snapshot são exclusivamente modelos injetados e nunca autorizam runtime.
