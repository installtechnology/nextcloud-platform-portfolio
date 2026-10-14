# Fixture S3/IMDS — preparação local, sem homologação

Esta fixture separada não é um adaptador operacional de backup. Nenhum container,
certificado, secret, volume ou bucket foi criado. O comando `python -m
scripts.backup.sandbox` valida somente o modelo estático; não aceita comandos
start/capture/restore. Todo resultado mantém runtime_allowed=false,
backup_complete=false e functional_restore=not_verified.

## Componentes preparados

- compose.json: modelo Docker Compose separado, allowlist de cinco serviços,
  Linux AMD64, imagens fixadas, pull_policy=never, redes db_internal e
  storage_internal internal=true, sem portas publicadas, host networking,
  socket Docker ou volumes operacionais. Auxiliares não root/read-only.
- imds.py: protocolo IMDSv2 fictício, token TTL, limite de tokens, exclusão mútua,
  credenciais aleatórias somente em RAM, sem upstream. Listener HTTPS só na futura
  invocação explícita, contexto sandbox/UID1000, chave TLS regular e privada.
- moto_server.py: início explícito somente no futuro container, exige Moto5.1.22
  e TLS; sem auto-instalação, reload, proxy ou download.
- platform.config.php/trust.ini/preflight: endpoint S3 HTTPS literal e IMDS HTTPS
  literal, CA validada, SDK3.376.2 conferido antes de instalar; rejeita fontes
  alternativas de credenciais. Certificados e secrets ainda não existem.
- install-sandbox.php: reutiliza productionInstallationAdapter e
  runPlatformInstallation do projeto. Substitui **somente bucket_empty**, apontando
  o SDK para o emulador HTTPS com CA explícita. Não altera a guarda de storage,
  segurança do banco/local data, lock, instalação ou confirmação final.
- validation.py: verificações puras sobre Compose e snapshots injetados. Não
  coleta Docker inspect nem comprova firewall/quiescência real. Uma declaração
  booleana de egress não é atestado do host nem autorização de execução.
- trial-plan.json: dezesseis cenários pendentes; nenhum é executor de captura.

## Dependências e origem

Moto5.1.22: ghcr.io/getmoto/motoserver, manifesto LinuxAMD64
sha256:117238c6e7e3b566387c4c74e6fb0b6fdc34a094920c2154cf06f11dc72d9f37.
Índice: sha256:1e3802c95726373544967b428201c548f0247c15b00db2d96a5ba0a77d8643b8.
[Release oficial](https://github.com/getmoto/moto/releases/tag/5.1.22).
Consultados manifestos públicos, não camadas, assinatura ou vulnerabilidades.
Moto é emulador de S3, **não avaliador completo de IAM/bucket policies da AWS**.

Nextcloud34.0.4/PG17.11/Redis7.4.8: os três pins são exatamente os do Compose
operacional. Os três manifestos fixados existem e incluem AMD64; Redis também
é conferido pela validação de snapshot. A tag mutável Nextcloud34.0.4-apache
aponta atualmente para outro digest; o pin aprovado foi preservado. Conferir
versões/SDK/config e bytes das imagens após download autorizado, sem fallback.

[Auditoria e evidências](../../../docs/phase-7-s3-imds-compatibility-audit.md).

## Testes locais sem instalação

Usar Python existente e as dependências previamente aprovadas dos PRs anteriores.

```powershell
# Fonte oficial previamente obtida/revista, fora do Git; nunca download automático.
$env:SANDBOX_TEST_MOTO_SOURCE = '<caminho-local-da-fonte-moto-5.1.22/models.py>'
$env:CAPTURE_TEST_PHP = '<executavel-PHP-existente-e-aprovado>'
python -m unittest discover -s scripts/backup/sandbox/tests -v
python -m scripts.backup.sandbox
```

O teste da fonte exige o SHA-256
b25f031c5e785212801af5aec1b8d1732736478a424c59a0383f5f1e52a20175.
Extrai somente a função list_object_versions oficial e a executa com dependências
sintéticas. Não importa Moto nem executa o servidor. Sem fonte explícita, esse
grupo é pulado; nunca considerar tal execução aprovação da paginação.
Não há wheel/dependência nova. As credenciais do teste IMDS são sintéticas e
nunca impressas; os testes não abrem listener/socket.

## Primeira execução futura: autorização separada

1. VM Ubuntu24.04 x64 exclusiva, Docker/systemd após autorização; ambiente sem
   AWS, SSH operacional, profiles, proxies ou pastas do operador. Só dados sintéticos.
2. Download autorizado das quatro imagens, conferência de índice/manifesto/IDs,
   dependências efetivas, SDK, versão PHP/Python e non-root/TLS/read-only.
3. Revisão do executor/bundle ainda pendente: copiar fontes públicas revistadas
   para raiz privada0700, hash conferido. O bundle mapeia `install-nextcloud.php`
   original para /opt/platform/install-original.php, wrapper para
   /opt/platform/install-nextcloud.php; platform.config de sandbox substitui
   somente a configuração do sandbox. Originais configure/guard/healthcheck,
   postgres-init e redis-entrypoint são cópias verificadas, não arquivos editados.
4. Gerar CA, certificados SAN s3.sandbox.test/fixture-data.s3.sandbox.test e
   imds.sandbox.test, e secrets sintéticos novos. Chaves0600, ownership UID1000
   dos servidores, passwords acessíveis somente aos consumidores; validar ACLs
   reais de Compose secrets antes de iniciar. Nada armazenado no repositório.
5. Precarregar imagens; bloquear egress/link-localIPv4IPv6 no host e containers,
   comprovar controles com testes negativos independentes. Redes internal por
   si só não atestam isolamento completo do daemon/host. Sem host networking
   nem portas públicas. Moto guarda dados em RAM e perde VersionIds após restart.
6. Antes do instalador, criar/versionar **somente** fixture-data no emulador.
   Não há inicialização automática do bucket. Fazer gates TLS/IMDS e a allowlist.
7. Ensaios de trial-plan.json só após revisão da supervisão/fencing e autorização.
   Captura original PR3 permanece limitada a três serviços e rejeita AWS_*;
   não passar esta fixture ao backend padrão. Adapter de captura separado,
   monitor contínuo e coletor de snapshots **ainda não implementados**.

## Limites das garantias

Inventário idêntico antes/depois não comprova ausência de escritores. Exigir
controle contínuo de requisições, cron, CLI, sessões PG, redes e consumidores;
qualquer escritor externo, saída ambígua ou perda do lease invalida a janela.
SIGKILL pode deixar docker exec vivo: supervisão e recuperação precisam ser
homologadas na VM. Nunca liberar manutenção alheia ou reaproveitar estado parcial.

TLS handshake, S3 via SDK/IMDS, instalação real, dump, falhas/reboot e restauração
independente estão pendentes. Os testes POSIX já aceitos não cobrem esses ensaios.
Nenhum manifesto, checksum, stub ou modelo concede autorização de Destroy.
Recursos iniciais propostos: VM4vCPU/8GiB/40GiB para dados pequenos, a medir.
Custo apenas VM/disco/energia/downloads autorizados; nenhum recurso AWS novo.


## Auditoria final local

Os 41 testes originais foram preservados; a suíte agora contém 59 testes, incluindo
regressões de namespace/DNS, contexto/identidade, origem do bundle, mounts,
auxiliares non-root/read-only e wrapper PHP com SDK falso. O harness PHP adapta
somente paths de arquivos para o diretório temporário Windows e injeta uma factory
sintética; executa o algoritmo original e confirma identidade dos callbacks não
substituídos. Isso não prova instalação com SDK/serviços reais ou handshake TLS.

O sandbox exige PLATFORM_SYNTHETIC_SANDBOX=s3-imds-v1 e PLATFORM_SANDBOX_PROJECT
no namespace nc-s3-imds-test com 16 dígitos hexadecimais. O modelo Compose contém
um placeholder; snapshots runtime rejeitam placeholder não expandido ou identidade
ausente. A raiz do bundle deve ter o mesmo nome do projeto, ficar fora do checkout
operacional e não atravessar symlinks; binds são RO, arquivos regulares e destinos
exatos do modelo ou secrets autorizados. Volumes devem pertencer ao namespace do
projeto e ter os destinos esperados. DNS/extra_hosts/namespaces container:* e
proxies são recusados. Auxiliares exigem UID1000, read-only, CapDrop ALL e NNP.

**Não executar compose.json diretamente.** Não existe launcher aprovado que
valide root, permissões, rotas, firewall ou daemon antes de criar containers.
O Compose bruto não valida SANDBOX_ROOT; a checagem do snapshot não substitui
esse futuro preflight. Dados booleanos de isolamento no modelo não comprovam
bloqueio de egress, ausência de IMDS real ou fencing. Um operador com controle do
daemon pode contornar um template: VM exclusiva e preflight independente continuam
pré-requisitos. A publicação poderá ser somente preparatória, nunca autorização
de execução, conclusão de backup ou Destroy.
